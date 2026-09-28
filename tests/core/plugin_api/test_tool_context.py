"""ToolContext is usable after importing only the plugin API types."""

import subprocess
import sys


def test_tool_context_builds_without_any_adapter_import() -> None:
    code = (
        "from pathlib import Path\n"
        "from chipgraph.core.plugin_api.types import ToolContext\n"
        "class R:\n"
        "    name = 'fake'\n"
        "    async def run(self, cmd, *, cwd, env=None, timeout_s=None): ...\n"
        "ToolContext(repo_root=Path('.'), runner=R())\n"
        "import sys\n"
        "assert not any(m.startswith('chipgraph.adapters') for m in sys.modules)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
