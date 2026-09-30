"""S7 spike: check the harness along the real run's path, without calling any model.

    uv run python docs/spikes/s7/selftest.py

Builds the project with `setup.sh` (the same script `run.sh` uses), starts `s7_server.py`
as a stdio subprocess from the generated `mcp.json` (as Claude Code would), feeds
`hook_guard.py` real hook JSON on stdin, and plays the agents' writes. Costs no Claude
usage.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

from mcp import Client, StdioServerParameters

HERE = Path(__file__).resolve().parent
GOOD_PULSE = """module tiny_pulse (
    input  logic clk,
    input  logic rst_n,
    input  logic in,
    output logic pulse
);
  logic in_q;
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) in_q <= 1'b0;
    else        in_q <= in;
  end
  assign pulse = in & ~in_q;
endmodule
"""


def _data(result: object) -> dict[str, object]:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)  # type: ignore[return-value]
    return dict(json.loads(result.content[0].text))  # type: ignore[attr-defined]


def _hook(root: Path, event: object) -> tuple[int, str]:
    """Run the guard as Claude Code does: JSON on stdin, project dir in the env."""
    stdin = event if isinstance(event, str) else json.dumps(event)
    proc = subprocess.run(
        ["python3", str(HERE / "hook_guard.py")],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(root)},
        check=False,
    )
    return proc.returncode, proc.stdout


def _denied(out: str) -> bool:
    return '"permissionDecision": "deny"' in out


def _write(root: Path, rel: str) -> dict[str, object]:
    return {"tool_name": "Write", "tool_input": {"file_path": str(root / rel), "content": "x"}}


async def run(out: Path) -> None:
    root = out / "tinysoc"
    server = json.loads((out / "mcp.json").read_text())["mcpServers"]["chipgraph"]
    params = StdioServerParameters(command=server["command"], args=server["args"])
    async with Client(params) as client:
        # The committed tree is clean: .claude/ is part of the first commit.
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True
        ).stdout
        assert status == "", status

        tasks = _data(await client.call_tool("next_task", {}))
        assert [t["task_id"] for t in tasks["tasks"]] == ["t1", "t2"], tasks  # type: ignore[index, union-attr]

        code, stdout = _hook(root, _write(root, "rtl/tiny_pulse.sv"))
        assert code == 0 and stdout.strip() == "", (code, stdout)  # allowed: no decision
        code, stdout = _hook(root, _write(root, "README.md"))
        assert code == 0 and _denied(stdout), (code, stdout)
        code, stdout = _hook(root, {"tool_name": "Write", "tool_input": {"file_path": "/etc/x"}})
        assert _denied(stdout)
        code, stdout = _hook(root, {"tool_name": "Read", "tool_input": {"file_path": "/etc/x"}})
        assert code == 0 and stdout.strip() == ""

        # Fail closed: bad stdin, and a broken allowed.json, both refuse the write.
        code, stdout = _hook(root, "not json")
        assert code == 2 and _denied(stdout), (code, stdout)
        allowed = root / ".s7" / "allowed.json"
        saved = allowed.read_text()
        allowed.write_text("{broken")
        code, stdout = _hook(root, _write(root, "rtl/tiny_pulse.sv"))
        assert code == 2 and _denied(stdout), (code, stdout)
        allowed.write_text(saved)

        # An agent's write inside outputs, then submit: accepted even though .claude/ and
        # .s7/ exist in the tree.
        (root / "rtl" / "tiny_pulse.sv").write_text(GOOD_PULSE)
        (root / ".claude" / "settings.local.json").write_text("{}")
        ok = _data(await client.call_tool("submit", {"task_id": "t1"}))
        assert ok["accepted"] is True, ok

        (root / "README.md").write_text("changed outside outputs\n")
        bad = _data(await client.call_tool("submit", {"task_id": "t1"}))
        assert bad["accepted"] is False and bad["outside_outputs"] == ["README.md"], bad
        missing = _data(await client.call_tool("submit", {"task_id": "t2"}))
        assert missing["missing"] == ["rtl/tiny_sat.sv"], missing

    log = [json.loads(x) for x in (root / ".s7" / "hook.log").read_text().splitlines()]
    assert [e["decision"] for e in log] == ["allow", "deny", "deny", "none"], log
    print("selftest ok: setup.sh tree, stdio server, hook allow/deny/fail-closed, submit")


def _refuses(out: str) -> bool:
    proc = subprocess.run([str(HERE / "setup.sh"), out], capture_output=True, text=True)
    return (proc.returncode == 2 and "refusing" in proc.stderr + proc.stdout) or (
        proc.returncode == 2 and "inside the repo" in proc.stderr
    )


def main() -> None:
    assert _refuses("/Users/nobody/important"), "setup.sh must refuse a dir outside /tmp"
    assert _refuses(str(HERE.parents[2] / "s7-out")), "setup.sh must refuse a dir in the repo"
    with tempfile.TemporaryDirectory(prefix="s7-") as tmp:
        out = Path(tmp) / "run"
        subprocess.run([str(HERE / "setup.sh"), str(out)], check=True)
        asyncio.run(run(out))


if __name__ == "__main__":
    main()
