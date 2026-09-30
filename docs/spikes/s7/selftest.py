"""S7 spike: check the server and the hook without calling any model.

    uv run python docs/spikes/s7/selftest.py

Plays the part of the agents: takes the tasks, writes an allowed and a refused file, and
submits. Costs no Claude usage.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp import Client

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hook_guard  # noqa: E402
import s7_server  # noqa: E402

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
    text = result.content[0].text  # type: ignore[attr-defined]
    return dict(json.loads(text))


async def run(root: Path) -> None:
    async with Client(s7_server.build(root)) as client:
        tasks = _data(await client.call_tool("next_task", {}))
        ids = [t["task_id"] for t in tasks["tasks"]]  # type: ignore[index, union-attr]
        assert ids == ["t1", "t2"], tasks
        ctx = _data(await client.call_tool("get_context", {"task_id": "t1"}))
        assert ctx["outputs"] == ["rtl/tiny_pulse.sv"]

        allow = hook_guard.decide(
            {"tool_name": "Write", "tool_input": {"file_path": str(root / "rtl/tiny_pulse.sv")}},
            root,
        )
        deny = hook_guard.decide(
            {"tool_name": "Edit", "tool_input": {"file_path": str(root / "README.md")}}, root
        )
        outside = hook_guard.decide(
            {"tool_name": "Write", "tool_input": {"file_path": "/etc/passwd"}}, root
        )
        read = hook_guard.decide({"tool_name": "Read", "tool_input": {}}, root)
        assert allow[0] == "allow" and deny[0] == "deny" and outside[0] == "deny", (allow, deny)
        assert read[0] == "none"

        (root / "rtl" / "tiny_pulse.sv").write_text(GOOD_PULSE)
        ok = _data(await client.call_tool("submit", {"task_id": "t1"}))
        assert ok["accepted"] is True, ok

        (root / "README.md").write_text("changed outside outputs\n")
        bad = _data(await client.call_tool("submit", {"task_id": "t1"}))
        assert bad["accepted"] is False and bad["outside_outputs"] == ["README.md"], bad
        missing = _data(await client.call_tool("submit", {"task_id": "t2"}))
        assert missing["missing"] == ["rtl/tiny_sat.sv"], missing
    print("selftest ok: next_task, get_context, hook allow/deny, submit accept/reject")


def main() -> None:
    repo = HERE.parents[2]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "tinysoc"
        shutil.copytree(repo / "examples" / "tinysoc", root)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=s7", "-c", "user.email=s7@x", "commit", "-qm", "x"],
            cwd=root,
            check=True,
        )
        asyncio.run(run(root.resolve()))


if __name__ == "__main__":
    main()
