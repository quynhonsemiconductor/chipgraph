"""`TbStaticCheck`: a cocotb test read without a simulator and without the RTL (M2-06).

The check the `dv/tb_module` rule runs on the testbench Author's test file. It is
deterministic and never runs a simulator (unless the profile asks for one, see `sim`
below); it reads only the test file, the Design Model and, when the model lacks a
module's ports, that module's declaration (`chipgraph.packs.dv.interface`, the same
interface the Author's context shows). Issues are on the test file, as `file:line`:

- `tb_static.missing`: the test file does not exist;
- `tb_static.syntax`: it does not compile (`ast.parse`);
- `tb_static.no_cocotb`: it does not import `cocotb`;
- `tb_static.no_test`: no `@cocotb.test()` coroutine (`tb_static.not_async`: a
  `@cocotb.test()` on a plain `def`);
- `tb_static.unknown_port`: a `dut.<name>` access (or `getattr(dut, "<name>")`) whose
  name is not a port of the interface nor an allowed handle attribute (`_log`, `_name`,
  `_path`, and the `allow` arg's names, for a documented clock/reset convention);
  `tb_static.dynamic_access`: `getattr(dut, <not a literal>)`, `dut._id(...)` and other
  lookups by a computed name. When no interface is known the port check is skipped
  with one `warning`;
- `tb_static.forbidden`: anything that could read the RTL or run a program: `open()`,
  `exec`/`eval`/`compile`/`__import__`, `os.system`/`os.popen`/`os.exec*`/`os.spawn*`/
  `os.listdir`/`os.scandir`/`os.walk`, `.read_text()`/`.read_bytes()`/`.open()`,
  importing `subprocess`, `glob`, `pathlib`, `shutil`, `io`, `fileinput`, `linecache`,
  `importlib`, `codecs`, `builtins`, `pty`, and string literals naming an RTL file
  (`.sv`, `.svh`, `.v`, `.vh`) or an `rtl/` path;
- requirements (from the Design Model, the block's): every id must appear in a
  `# verifies: <ID>[, <ID>...]` comment or in a `@cocotb.test()` docstring at least once
  (`tb_static.req_missing`, a `warning`); an id cited there that is not one of the
  model's requirements is an `error` (`tb_static.req_unknown`).

A `fail` has at least one `error` issue; warnings alone pass.

Profile example (`.chipgraph.yml`)::

    adapters:
      tb_static:
        use: tb_static
        test: "dv/{block}/test_{block}.py"   # the default
        top: "tiny_{block}"                  # the module, when the model has none
        filelist: "filelists/{block}.f"      # where its declaration is (default: layout)
        allow: [clk_i]                       # extra dut names (clock/reset conventions)
        sim:                                 # optional: run the test on the RTL too
          use: edalize
          simulator: verilator
          top: "tiny_{block}"
          filelist: "filelists/{block}.f"
          test_module: "dv/{block}/test_{block}.py"

`sim` (optional, per profile): an inline adapter config. Only when every static rule
passed, the named tool adapter (`edalize`, or any check/tool adapter) runs with these
args and its result becomes this check's: its status, and its issues after the static
ones. The engine shows a role that must not see RTL only what it may of that result
(`FeedbackFilter`).
"""

from __future__ import annotations

import ast
import io
import re
import time
import tokenize
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from chipgraph.checks._common import (
    ArgError,
    compute_idempotency_key,
    error_result,
    optional_list_of_str,
    optional_str,
    sha256_file,
)
from chipgraph.checks._model import ModelUnavailable, load_model
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus, Severity
from chipgraph.core.model.model import DesignModel
from chipgraph.core.plugin_api.registry import PluginError, Registry
from chipgraph.core.plugin_api.types import ToolContext
from chipgraph.packs.dv.interface import Interface, block_requirements, project_interface

DEFAULT_TEST = "dv/{block}/test_{block}.py"
"""Where `dv/tb_module` writes a block's test."""

HANDLE_ATTRS = frozenset({"_log", "_name", "_path"})
"""Attributes of a cocotb handle a test may use besides the ports."""

FORBIDDEN_MODULES = frozenset(
    {
        "subprocess",
        "glob",
        "pathlib",
        "shutil",
        "io",
        "fileinput",
        "linecache",
        "importlib",
        "codecs",
        "builtins",
        "pty",
    }
)
FORBIDDEN_CALLS = frozenset({"open", "exec", "eval", "compile", "__import__"})
FORBIDDEN_OS = frozenset({"system", "popen", "listdir", "scandir", "walk", "startfile"})
FORBIDDEN_OS_PREFIXES = ("exec", "spawn", "posix_spawn")
FORBIDDEN_METHODS = frozenset({"read_text", "read_bytes", "open", "iterdir", "glob", "rglob"})
_RTL_LITERAL = re.compile(r"(?:\.(?:sv|svh|v|vh)\b|(?:^|[/\\])rtl[/\\])", re.IGNORECASE)
_VERIFIES = re.compile(r"#\s*verifies\s*:\s*(?P<ids>.+)$", re.IGNORECASE)
_ID_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)+")
_ID_LIKE = re.compile(r"^[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)*-\d+$")


def _fill(template: str, params: Mapping[str, str]) -> str:
    out = template
    for key, value in params.items():
        out = out.replace("{" + key + "}", value).replace("{" + key.upper() + "}", value.upper())
    return out


def _issue(file: str, line: int | None, rule: str, msg: str, severity: Severity = "error") -> Issue:
    return Issue(file=file, line=line, rule=rule, severity=severity, msg=msg)


# --- what the test file does -------------------------------------------------------------


def _is_cocotb_test(decorator: ast.expr) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "test"
        and isinstance(target.value, ast.Name)
        and target.value.id == "cocotb"
    )


def _tests(tree: ast.Module) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            _is_cocotb_test(d) for d in node.decorator_list
        ):
            yield node


def _imports_cocotb(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            a.name == "cocotb" or a.name.startswith("cocotb.") for a in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "cocotb":
            return True
    return False


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def _forbidden(tree: ast.Module, rel: str) -> list[Issue]:
    issues: list[Issue] = []
    os_names = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in FORBIDDEN_MODULES:
                    issues.append(
                        _issue(rel, node.lineno, "tb_static.forbidden", f"import of {alias.name!r}")
                    )
                if top == "os" and alias.asname:
                    os_names.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = module.split(".")[0]
            if top in FORBIDDEN_MODULES:
                issues.append(
                    _issue(rel, node.lineno, "tb_static.forbidden", f"import from {module!r}")
                )
            if top == "os":
                for alias in node.names:
                    if alias.name in FORBIDDEN_OS or alias.name.startswith(FORBIDDEN_OS_PREFIXES):
                        issues.append(
                            _issue(
                                rel,
                                node.lineno,
                                "tb_static.forbidden",
                                f"import of os.{alias.name}",
                            )
                        )
        elif isinstance(node, ast.Call):
            func = node.func
            name = _dotted(func)
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
                issues.append(
                    _issue(rel, node.lineno, "tb_static.forbidden", f"call of {func.id}()")
                )
            elif isinstance(func, ast.Attribute):
                base = _dotted(func.value)
                if base in os_names and (
                    func.attr in FORBIDDEN_OS or func.attr.startswith(FORBIDDEN_OS_PREFIXES)
                ):
                    issues.append(
                        _issue(rel, node.lineno, "tb_static.forbidden", f"call of {name}()")
                    )
                elif func.attr in FORBIDDEN_METHODS:
                    issues.append(
                        _issue(
                            rel,
                            node.lineno,
                            "tb_static.forbidden",
                            f"call of .{func.attr}(): a test reads no file",
                        )
                    )
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _RTL_LITERAL.search(node.value)
        ):
            issues.append(
                _issue(
                    rel,
                    node.lineno,
                    "tb_static.forbidden",
                    "a string naming an RTL file or an rtl/ path: a test never reads the RTL",
                )
            )
    return issues


def _dut_names(tree: ast.Module) -> set[str]:
    """The names a test calls the DUT handle: `dut`, and the first argument of each
    `@cocotb.test()` coroutine."""
    names = {"dut"}
    for node in _tests(tree):
        if node.args.args:
            names.add(node.args.args[0].arg)
    return names


def _port_issues(tree: ast.Module, rel: str, allowed: frozenset[str]) -> list[Issue]:
    issues: list[Issue] = []
    duts = _dut_names(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in duts
        ):
            name = node.attr
            if name in allowed or name in HANDLE_ATTRS:
                continue
            if name.startswith("_"):
                issues.append(
                    _issue(
                        rel,
                        node.lineno,
                        "tb_static.dynamic_access",
                        f"{node.value.id}.{name}: a lookup into the design's internals; use "
                        "only the interface's ports",
                    )
                )
                continue
            issues.append(
                _issue(
                    rel,
                    node.lineno,
                    "tb_static.unknown_port",
                    f"{node.value.id}.{name} is not a port of the interface "
                    f"(ports: {', '.join(sorted(allowed)) or 'none'}); a test uses only the "
                    "interface, never the design's internals",
                )
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("getattr", "hasattr", "setattr")
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in duts
        ):
            key = node.args[1] if len(node.args) > 1 else None
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                if key.value not in allowed and key.value not in HANDLE_ATTRS:
                    issues.append(
                        _issue(
                            rel,
                            node.lineno,
                            "tb_static.unknown_port",
                            f"{node.func.id}(dut, {key.value!r}): not a port of the interface",
                        )
                    )
            else:
                issues.append(
                    _issue(
                        rel,
                        node.lineno,
                        "tb_static.dynamic_access",
                        f"{node.func.id}(dut, <computed name>): name the interface's ports "
                        "directly",
                    )
                )
    return issues


# --- requirements --------------------------------------------------------------------------


def _citations(text: str, tree: ast.Module) -> list[tuple[int, str]]:
    """Every (line, id) a test cites: in `# verifies:` comments, and id-like tokens in the
    docstrings of `@cocotb.test()` coroutines."""
    found: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                match = _VERIFIES.search(tok.string)
                if match:
                    for id_ in re.split(r"[\s,;]+", match.group("ids").strip()):
                        if id_:
                            found.append((tok.start[0], id_.strip(".")))
    except (tokenize.TokenError, SyntaxError):
        pass
    for node in _tests(tree):
        doc = ast.get_docstring(node, clean=False)
        if not doc or not node.body:
            continue
        line = node.body[0].lineno
        for match in _ID_TOKEN.finditer(doc):
            if _ID_LIKE.match(match.group(0)):
                found.append((line, match.group(0)))
    return found


def _requirement_issues(
    rel: str, text: str, tree: ast.Module, model: DesignModel | None, block: str | None
) -> list[Issue]:
    if model is None or block is None:
        return [
            _issue(
                rel,
                None,
                "tb_static.req_unchecked",
                "no Design Model (run `chipgraph ingest`): requirement citations not checked",
                "warning",
            )
        ]
    ids = [r["id"] for r in block_requirements(model, block)]
    known = {
        r.name for r in model.by_kind("requirement")
    }  # another block's id is not unknown, only stale ones are
    cited = _citations(text, tree)
    cited_ids = {c for _, c in cited}
    issues = [
        _issue(
            rel,
            line,
            "tb_static.req_unknown",
            f"{id_} is cited but is not a requirement of the Design Model",
        )
        for line, id_ in cited
        if id_ not in known
    ]
    issues.extend(
        _issue(
            rel,
            None,
            "tb_static.req_missing",
            f"requirement {id_} of block {block!r} is cited by no `# verifies:` comment or "
            "`@cocotb.test()` docstring",
            "warning",
        )
        for id_ in ids
        if id_ not in cited_ids
    )
    return issues


# --- the check -------------------------------------------------------------------------------


def _project_parts(root: Path, block: str | None) -> dict[str, Any]:
    """The profile's layout for `block` ({} when there is no readable profile)."""
    from chipgraph.core.config.errors import ConfigError
    from chipgraph.core.config.loader import load

    try:
        resolved = load(root)
    except (ConfigError, OSError, ValueError):
        return {}
    if resolved is None:
        return {}
    profile = resolved.for_block(block) if block is not None else resolved.profile
    return dict(profile.layout)


def static_issues(
    root: Path,
    rel: str,
    *,
    interface: Interface | None,
    model: DesignModel | None,
    block: str | None,
    allow: frozenset[str] = frozenset(),
) -> list[Issue]:
    """Every static issue of the test file `rel` (see the module docstring)."""
    path = root / rel
    if not path.is_file():
        return [_issue(rel, None, "tb_static.missing", f"test file {rel} does not exist")]
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError as exc:
        return [_issue(rel, exc.lineno, "tb_static.syntax", f"does not compile: {exc.msg}")]
    issues: list[Issue] = []
    if not _imports_cocotb(tree):
        issues.append(_issue(rel, None, "tb_static.no_cocotb", "the test does not import cocotb"))
    tests = list(_tests(tree))
    if not tests:
        issues.append(
            _issue(rel, None, "tb_static.no_test", "no `@cocotb.test()` coroutine in the file")
        )
    issues.extend(
        _issue(
            rel,
            t.lineno,
            "tb_static.not_async",
            f"{t.name}: a `@cocotb.test()` must be an `async def`",
        )
        for t in tests
        if not isinstance(t, ast.AsyncFunctionDef)
    )
    issues.extend(_forbidden(tree, rel))
    if interface is None or interface.source is None:
        issues.append(
            _issue(
                rel,
                None,
                "tb_static.no_interface",
                "no interface known for the module: port use not checked",
                "warning",
            )
        )
    else:
        names = {p.name for p in interface.ports} | {
            n for n in (interface.clock, interface.reset) if n
        }
        issues.extend(_port_issues(tree, rel, frozenset(names) | allow))
    issues.extend(_requirement_issues(rel, text, tree, model, block))
    return sorted(issues, key=lambda i: (i.line or 0, i.rule, i.msg))


class TbStaticCheck:
    """Reads a cocotb test file without a simulator and without the RTL."""

    id = "tb_static"
    name = "Testbench static check"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        args = spec.args
        try:
            test_template = optional_str(args, "test", DEFAULT_TEST)
            allow = frozenset(optional_list_of_str(args, "allow"))
            sim = args.get("sim")
            if sim is not None and not (
                isinstance(sim, Mapping) and isinstance(sim.get("use"), str)
            ):
                raise ArgError("'sim' must be an adapter config with a 'use' key")
        except ArgError as exc:
            return error_result(spec, str(exc), start)

        params = dict(ctx.params)
        block = params.get("block") or None
        rel = _fill(test_template, params)
        if "{" in rel:
            return error_result(spec, f"test path {rel!r} has an unfilled placeholder", start)
        model: DesignModel | None = None
        model_hash = ""
        try:
            loaded = load_model(ctx.repo_root)
            model, model_hash = loaded.model, loaded.build_inputs_hash or ""
        except ModelUnavailable:
            model = None
        interface = project_interface(
            ctx.repo_root,
            model=model,
            block=block,
            params=params,
            args=args,
            layout=_project_parts(ctx.repo_root, block),
        )
        issues = static_issues(
            ctx.repo_root, rel, interface=interface, model=model, block=block, allow=allow
        )
        status: CheckStatus = "fail" if any(i.severity == "error" for i in issues) else "pass"
        log_tail = (
            f"tb_static: {rel}: {sum(i.severity == 'error' for i in issues)} error(s), "
            f"{sum(i.severity == 'warning' for i in issues)} warning(s); interface "
            f"{interface.module or '?'} from {interface.source or 'nowhere'}"
        )
        if status == "pass" and sim is not None:
            sim_result = await _run_sim(dict(sim), ctx, spec.id)
            status = sim_result.status
            issues = [*issues, *sim_result.issues]
            log_tail = f"{log_tail}\n{sim_result.log_tail}".strip()
        path = ctx.repo_root / rel
        files = [(rel, sha256_file(path))] if path.is_file() else []
        files.append(("<model>", model_hash))
        return CheckResult(
            check_id=spec.id,
            status=status,
            issues=tuple(issues),
            log_tail=log_tail[-4000:],
            duration_s=time.monotonic() - start,
            idempotency_key=compute_idempotency_key(spec.id, args, files),
        )


async def _run_sim(cfg: dict[str, Any], ctx: ToolContext, check_id: str) -> CheckResult:
    """Run the inline `sim` adapter config with this check's context."""
    use = str(cfg.pop("use"))
    registry = Registry()
    try:
        registry.discover()
    except PluginError as exc:
        return _sim_error(check_id, f"cannot load the adapters: {exc}")
    plugin = None
    for kind in ("check", "tool"):
        try:
            plugin = registry.get(kind, use)
            break
        except PluginError:
            continue
    if plugin is None:
        return _sim_error(check_id, f"no check or tool adapter named {use!r} (tb_static.sim)")
    sim_spec = CheckSpec(id=f"{check_id}.sim", capability="sim", adapter=use, args=cfg)
    result: CheckResult = await plugin.run(sim_spec, ctx)
    return result


def _sim_error(check_id: str, msg: str) -> CheckResult:
    return CheckResult(
        check_id=f"{check_id}.sim",
        status="error",
        issues=(Issue(rule="tb_static.sim", msg=msg),),
        duration_s=0.0,
        idempotency_key=compute_idempotency_key(check_id, {"sim": msg}, ()),
    )


__all__ = ["DEFAULT_TEST", "TbStaticCheck", "static_issues"]
