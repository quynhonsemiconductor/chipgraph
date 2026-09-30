"""`ConnectCheck`: the top's RTL wiring against the chip contract (layer 1).

A layer-1 (structure) cross check (DESIGN.md 4.8). It reads the Design Model
`chipgraph ingest` wrote and parses the top module's RTL file with the pyslang syntax
tree (no elaboration), collecting the top's hierarchy instances and their named port
connections, then compares them to the contract:

* `instance.missing` -- a contract block that should appear under the top (or a D38
  `instance_of` instance) has no RTL instance of a module owned by its IP.
* `interrupt.wiring` -- a contract interrupt's output on its instance is not wired. The
  interrupt output is the instance port whose *spec* name matches arg `irq_port`
  (default `^o_int_|irq`). When the design declares an interrupt vector net (arg
  `irq_vector`), the output must connect to bit `line` of that vector; when it does not
  (as in the tinysoc example, whose single interrupt is a plain `timer_irq` output), the
  rule is only that the interrupt output is connected to *something*.

Wiring the check cannot resolve (an expression it does not understand) is reported as
`info`, never guessed at. A missing model is a whole-check `error`; a top file that
cannot be found or parsed is an `error` too.

The top module is the RTL module of the `top` block (arg `top_file` overrides the file).

Example profile usage (`.chipgraph.yml`):

```yaml
adapters:
  connect: { use: connect, irq_port: "^o_int_|irq", irq_vector: "o_int_vec" }
```
"""

from __future__ import annotations

import re
import time

from chipgraph.checks._common import (
    ArgError,
    compute_idempotency_key,
    error_result,
    optional_str,
    sha256_file,
)
from chipgraph.checks._model import ModelUnavailable, load_model
from chipgraph.checks._xref_syntax import Instance, parse_rtl
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus
from chipgraph.core.model.entities import ModuleEntity
from chipgraph.core.model.model import DesignModel
from chipgraph.core.plugin_api.types import ToolContext

_DEFAULT_IRQ_PORT = r"^o_int_|irq"
_TOP_BLOCK = "top"


class ConnectCheck:
    """Checks the top module's RTL instances and interrupt wiring against the contract."""

    id = "connect"
    name = "Connect"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        args = spec.args

        try:
            irq_port_pat = optional_str(args, "irq_port", _DEFAULT_IRQ_PORT)
            irq_vector = optional_str(args, "irq_vector", "")
            top_file_arg = optional_str(args, "top_file", "")
            top_block = optional_str(args, "top_block", _TOP_BLOCK)
            irq_port_re = re.compile(irq_port_pat)
        except (ArgError, re.error) as exc:
            return error_result(spec, str(exc), start)

        try:
            loaded = load_model(ctx.repo_root)
        except ModelUnavailable as exc:
            return error_result(spec, str(exc), start, rule="model")

        model = loaded.model
        top_rel = _top_file(model, top_block, top_file_arg)
        if top_rel is None:
            return error_result(
                spec,
                f"no top module file found for block {top_block!r}; set args['top_file']",
                start,
                rule="top",
            )
        top_path = ctx.repo_root / top_rel
        if not top_path.is_file():
            return error_result(spec, f"top file {top_rel!r} does not exist", start, rule="top")

        parsed = parse_rtl(top_path)
        issues: list[Issue] = [
            Issue(
                file=top_rel,
                line=pf.line,
                rule="parse",
                severity="error",
                msg=f"parse error: {pf.message}",
            )
            for pf in parsed.parse_errors
        ]

        issues.extend(
            _connect_issues(model, parsed.instances, top_rel, top_block, irq_port_re, irq_vector)
        )
        issues.sort(key=lambda i: (i.file or "", i.line or 0, i.rule, i.msg))

        file_hashes = [
            (top_rel, sha256_file(top_path)),
            ("<model>", loaded.build_inputs_hash or "none"),
        ]
        key = compute_idempotency_key(spec.id, args, file_hashes)
        status: CheckStatus = "fail" if any(i.severity == "error" for i in issues) else "pass"
        return CheckResult(
            check_id=spec.id,
            status=status,
            issues=tuple(issues),
            duration_s=time.monotonic() - start,
            idempotency_key=key,
        )


def _top_file(model: DesignModel, top_block: str, top_file_arg: str) -> str | None:
    """The repo-relative top RTL file: `top_file` arg wins, else the top block's module."""
    if top_file_arg:
        return top_file_arg
    top_key = f"block:{top_block}"
    modules = [
        e
        for e in model.by_kind("module")
        if isinstance(e, ModuleEntity) and e.block == top_key and e.file
    ]
    if not modules:
        return None

    # The module of the top block with the most instances is the integration top.
    def instance_count(module_key: str) -> int:
        return len(model.get_relations(src=module_key, kind="instantiates"))

    modules.sort(key=lambda m: (-instance_count(m.key), m.key))
    return modules[0].file


def _connect_issues(
    model: DesignModel,
    instances: list[Instance],
    top_rel: str,
    top_block: str,
    irq_port_re: re.Pattern[str],
    irq_vector: str,
) -> list[Issue]:
    issues: list[Issue] = []
    instantiated_modules = {inst.module for inst in instances}

    # instance.missing: every non-top contract block (and D38 instance) needs an RTL
    # instance of a module its IP owns.
    for block in sorted(model.by_kind("block"), key=lambda b: b.key):
        name = block.name
        if name == top_block:
            continue
        ip_key = _ip_key(model, block.key)
        owned = _owned_module_names(model, ip_key)
        if not owned:
            # No RTL module is attributed to this IP yet; cannot check structurally.
            issues.append(
                Issue(
                    file=top_rel,
                    rule="instance.info",
                    severity="info",
                    msg=f"block {name!r} owns no RTL module in the model; instance not checked",
                )
            )
            continue
        if owned.isdisjoint(instantiated_modules):
            issues.append(
                Issue(
                    file=top_rel,
                    rule="instance.missing",
                    severity="error",
                    msg=(
                        f"contract block {name!r} has no RTL instance in the top of a module "
                        f"it owns (expected one of {sorted(owned)})"
                    ),
                )
            )

    # interrupt.wiring: each contract interrupt output on its instance must be wired.
    issues.extend(_interrupt_issues(model, instances, top_rel, irq_port_re, irq_vector))
    return issues


def _ip_key(model: DesignModel, block_key: str) -> str:
    """The IP block key a block-as-instance points at via `instance_of`, else itself (D38)."""
    rels = model.get_relations(src=block_key, kind="instance_of")
    return rels[0].dst if rels else block_key


def _owned_module_names(model: DesignModel, block_key: str) -> set[str]:
    """The RTL module names owned by `block_key` (its `block` field)."""
    return {
        e.name
        for e in model.by_kind("module")
        if isinstance(e, ModuleEntity) and e.block == block_key
    }


def _interrupt_issues(
    model: DesignModel,
    instances: list[Instance],
    top_rel: str,
    irq_port_re: re.Pattern[str],
    irq_vector: str,
) -> list[Issue]:
    issues: list[Issue] = []
    for irq in sorted(model.by_kind("interrupt"), key=lambda e: e.key):
        block_key = getattr(irq, "block", None)
        if not isinstance(block_key, str):
            continue
        ip_key = block_key  # interrupts live on the IP block itself
        owned = _owned_module_names(model, ip_key)
        irq_ports = _spec_irq_ports(model, block_key, irq_port_re)
        if not irq_ports:
            issues.append(
                Issue(
                    file=top_rel,
                    rule="interrupt.info",
                    severity="info",
                    msg=(
                        f"interrupt {irq.name!r} of block {ip_key!r} has no spec port matching "
                        "the interrupt-port pattern; cannot check its wiring"
                    ),
                )
            )
            continue
        inst = _instance_of(instances, owned)
        if inst is None:
            # instance.missing already covers a wholly absent instance.
            continue
        conn = None
        matched_port = None
        for port in irq_ports:
            conn = inst.connection(port)
            if conn is not None:
                matched_port = port
                break
        if conn is None:
            issues.append(
                Issue(
                    file=top_rel,
                    line=inst.line,
                    rule="interrupt.wiring",
                    severity="error",
                    msg=(
                        f"interrupt output {sorted(irq_ports)} of instance {inst.name!r} "
                        f"(interrupt {irq.name!r}) is not connected by name in the top"
                    ),
                )
            )
            continue
        expr = conn.expr.strip()
        if not expr:
            issues.append(
                Issue(
                    file=top_rel,
                    line=conn.line,
                    rule="interrupt.wiring",
                    severity="error",
                    msg=(
                        f"interrupt output {matched_port!r} of instance {inst.name!r} "
                        f"(interrupt {irq.name!r}) is connected to nothing"
                    ),
                )
            )
            continue
        if irq_vector:
            issues.extend(
                _vector_bit_issue(irq, inst, matched_port or "", conn, top_rel, irq_vector)
            )
    return issues


def _vector_bit_issue(
    irq: object, inst: Instance, port: str, conn: object, top_rel: str, irq_vector: str
) -> list[Issue]:
    """Check the interrupt output connects to `irq_vector[line]`; `info` when unresolved."""
    line_no = getattr(irq, "line", None)
    expr = getattr(conn, "expr", "").strip()
    conn_line = getattr(conn, "line", None)
    if not isinstance(line_no, int):
        return [
            Issue(
                file=top_rel,
                line=conn_line,
                rule="interrupt.info",
                severity="info",
                msg=f"interrupt {getattr(irq, 'name', '?')!r} has no line number; not checked",
            )
        ]
    m = re.fullmatch(rf"{re.escape(irq_vector)}\s*\[\s*(\d+)\s*\]", expr)
    if m is None:
        return [
            Issue(
                file=top_rel,
                line=conn_line,
                rule="interrupt.info",
                severity="info",
                msg=(
                    f"interrupt output {port!r} of {inst.name!r} connects to {expr!r}, "
                    f"which is not a resolvable bit of {irq_vector!r}; not checked further"
                ),
            )
        ]
    if int(m.group(1)) != line_no:
        return [
            Issue(
                file=top_rel,
                line=conn_line,
                rule="interrupt.wiring",
                severity="error",
                msg=(
                    f"interrupt {getattr(irq, 'name', '?')!r} is line {line_no} in the contract "
                    f"but wired to {irq_vector}[{m.group(1)}]"
                ),
            )
        ]
    return []


def _spec_irq_ports(model: DesignModel, block_key: str, irq_port_re: re.Pattern[str]) -> set[str]:
    """Spec output ports of `block_key` whose name matches the interrupt-port pattern."""
    out: set[str] = set()
    for port in model.by_kind("port"):
        if port.attrs.get("block") != block_key:
            continue
        if port.attrs.get("origin") != "spec":
            continue
        if getattr(port, "direction", None) != "output":
            continue
        if irq_port_re.search(port.name):
            out.add(port.name)
    return out


def _instance_of(instances: list[Instance], module_names: set[str]) -> Instance | None:
    for inst in instances:
        if inst.module in module_names:
            return inst
    return None


__all__ = ["ConnectCheck"]
