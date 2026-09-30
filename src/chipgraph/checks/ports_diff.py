"""`PortsDiffCheck`: an IP's spec ports against the RTL ports of its top module (layer 1).

For each IP block, compares the spec's declared ports (`port:spec.<block>.*`, D36/M1-05)
to the ports of the block's top RTL module (DESIGN.md 4.4 "Spec và RTL", 4.8 layer 1):

- the top module is the one module owned by the block (`ModuleEntity.block`) that no
  other module owned by the block instantiates; if there is not exactly one, a name
  template (arg `top`, e.g. `tiny_{block}`) picks it; if none can be found, one `info`
  issue is emitted for the block and it is not failed;
- reports a port missing in RTL, a port missing in the spec, a direction mismatch, and a
  width mismatch (integer widths only; a string width is skipped).

With a `--block`, only that IP (or an instance's IP) is compared.
"""

from __future__ import annotations

import time

from chipgraph.checks._common import ArgError, error_result, optional_str
from chipgraph.checks._cross import (
    block_param,
    load_model_or_skip,
    result_from_issues,
)
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.model.entities import ModuleEntity, PortEntity
from chipgraph.core.model.model import DesignModel
from chipgraph.core.plugin_api.types import ToolContext


class PortsDiffCheck:
    """Checks an IP's spec ports against the ports of its top RTL module."""

    id = "ports_diff"
    name = "Ports diff"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        try:
            top_template = optional_str(spec.args, "top", "")
        except ArgError as exc:
            return error_result(spec, str(exc), start)

        loaded = load_model_or_skip(spec, ctx, start)
        if isinstance(loaded, CheckResult):
            return loaded
        model = loaded.model
        block = block_param(ctx)

        issues: list[Issue] = []
        for ip in _ips_with_spec_ports(model):
            if block is not None and ip != block:
                continue
            issues.extend(_diff_block(model, ip, top_template))
        return result_from_issues(spec, issues, loaded, start)


def _ips_with_spec_ports(model: DesignModel) -> list[str]:
    """Every IP block name that has at least one spec port, sorted."""
    ips: set[str] = set()
    for port in model.by_kind("port"):
        assert isinstance(port, PortEntity)
        ip = _spec_port_block(port)
        if ip is not None:
            ips.add(ip)
    return sorted(ips)


def _spec_port_block(port: PortEntity) -> str | None:
    _, _, rest = port.key.partition(":")
    parts = rest.split(".")
    if len(parts) >= 3 and parts[0] == "spec":
        return parts[1]
    return None


def _spec_port_name(port: PortEntity) -> str:
    _, _, rest = port.key.partition(":")
    return rest.split(".", 2)[2]


def _diff_block(model: DesignModel, ip: str, top_template: str) -> list[Issue]:
    top = _top_module(model, ip, top_template)
    if top is None:
        return [
            Issue(
                file=None,
                line=None,
                rule="ports_diff.no_top",
                severity="info",
                msg=(
                    f"no single top RTL module for IP {ip!r}; set the check's `top` template "
                    "(e.g. 'tiny_{block}') to compare its spec ports to RTL"
                ),
            )
        ]

    spec_ports = {
        _spec_port_name(p): p
        for p in model.by_kind("port")
        if isinstance(p, PortEntity) and _spec_port_block(p) == ip
    }
    rtl_ports = {
        p.name: p
        for p in model.by_kind("port")
        if isinstance(p, PortEntity) and getattr(p, "module", None) == top.key
    }

    issues: list[Issue] = []
    for name in sorted(set(spec_ports) | set(rtl_ports)):
        spec_port = spec_ports.get(name)
        rtl_port = rtl_ports.get(name)
        if spec_port is not None and rtl_port is None:
            issues.append(
                Issue(
                    file=spec_port.source.file,
                    line=spec_port.source.line,
                    rule="ports_diff.missing_in_rtl",
                    severity="error",
                    msg=f"spec port {name!r} of IP {ip!r} is missing from RTL module {top.name!r}",
                )
            )
            continue
        if rtl_port is not None and spec_port is None:
            issues.append(
                Issue(
                    file=rtl_port.source.file,
                    line=rtl_port.source.line,
                    rule="ports_diff.missing_in_spec",
                    severity="error",
                    msg=(
                        f"RTL port {name!r} of module {top.name!r} is missing from IP {ip!r}'s spec"
                    ),
                )
            )
            continue
        assert spec_port is not None and rtl_port is not None
        if (
            spec_port.direction is not None
            and rtl_port.direction is not None
            and spec_port.direction != rtl_port.direction
        ):
            issues.append(
                Issue(
                    file=rtl_port.source.file,
                    line=rtl_port.source.line,
                    rule="ports_diff.direction",
                    severity="error",
                    msg=(
                        f"port {name!r} of IP {ip!r}: spec direction {spec_port.direction!r} "
                        f"but RTL {rtl_port.direction!r}"
                    ),
                )
            )
        if (
            isinstance(spec_port.width, int)
            and isinstance(rtl_port.width, int)
            and spec_port.width != rtl_port.width
        ):
            issues.append(
                Issue(
                    file=rtl_port.source.file,
                    line=rtl_port.source.line,
                    rule="ports_diff.width",
                    severity="error",
                    msg=(
                        f"port {name!r} of IP {ip!r}: spec width {spec_port.width} "
                        f"but RTL width {rtl_port.width}"
                    ),
                )
            )
    return issues


def _top_module(model: DesignModel, ip: str, top_template: str) -> ModuleEntity | None:
    """The block's top RTL module, or None when it cannot be identified.

    The top is the one module owned by the block that no other module owned by the block
    instantiates. If there is not exactly one such module, a `top` name template (with
    `{block}` substituted) picks a module by name.
    """
    ip_key = f"block:{ip}"
    owned = [
        m for m in model.by_kind("module") if isinstance(m, ModuleEntity) and m.block == ip_key
    ]
    if owned:
        instantiated = {
            r.dst for m in owned for r in model.get_relations(src=m.key, kind="instantiates")
        }
        roots = [m for m in owned if m.key not in instantiated]
        if len(roots) == 1:
            return roots[0]

    if top_template:
        want = top_template.replace("{block}", ip)
        for module in model.by_kind("module"):
            if isinstance(module, ModuleEntity) and module.name == want:
                return module
    return None


__all__ = ["PortsDiffCheck"]
