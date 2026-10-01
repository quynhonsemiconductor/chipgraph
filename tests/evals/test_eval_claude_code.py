"""M1-17: the `claude-code` runtime, driven against a scripted stand-in for `claude`.

No model is called: `CHIPGRAPH_EVAL_CLAUDE` points the runtime at a small script that logs
its command line and environment and prints a canned stream-json run. This checks the
command each sample gets, the auth, the per-sample budget flag, the suite budget and time
limit, and the stream parsing; what a real model answers is checked by the real runs.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO / "evals" / "harness"


def _load_harness() -> ModuleType:
    if "chipgraph_evals" in sys.modules:
        return sys.modules["chipgraph_evals"]
    spec = importlib.util.spec_from_file_location(
        "chipgraph_evals", HARNESS / "__init__.py", submodule_search_locations=[str(HARNESS)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["chipgraph_evals"] = module
    spec.loader.exec_module(module)
    return module


evals = _load_harness()
runtimes = evals.runtimes
streams = evals.streams

FAKE_CLAUDE = r"""#!{python}
import json, os, sys

args = sys.argv[1:]
if args == ["--help"]:
    print("  --max-budget-usd <amount>  Maximum dollar amount")
    sys.exit(0)
if args == ["--version"]:
    print("0.0.0 (fake Claude Code)")
    sys.exit(0)
prompt = args[args.index("-p") + 1]
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    record = {
        "argv": args,
        "cwd": os.getcwd(),
        "token": "CLAUDE_CODE_OAUTH_TOKEN" in os.environ,
        "api_key": "ANTHROPIC_API_KEY" in os.environ,
        "venv": "VIRTUAL_ENV" in os.environ,
        "profile": open(".chipgraph.yml").read(),
        "mcp": json.load(open(os.path.join(args[args.index("--plugin-dir") + 1], ".mcp.json"))),
    }
    f.write(json.dumps(record) + "\n")
cost = float(os.environ.get("FAKE_CLAUDE_COST", "0.5"))
labels = json.loads(os.environ.get("FAKE_CLAUDE_LABELS", "{}"))


def emit(event):
    print(json.dumps(event), flush=True)


def call(tid, name, args, result, parent=None):
    emit({"type": "assistant", "parent_tool_use_id": parent, "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": args}]}})
    emit({"type": "user", "parent_tool_use_id": parent, "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid,
         "content": [{"type": "text", "text": json.dumps(result)}]}]}})


emit({"type": "system", "subtype": "init"})
tool = "mcp__plugin_chipgraph_chipgraph__"
if prompt.startswith("/chipgraph:triage "):
    log = prompt.split()[1]
    sid = os.path.basename(log)[: -len(".log")]
    assert os.path.isfile(log), log
    call("a1", "Agent", {"subagent_type": "chipgraph:decider", "model": "haiku"}, {})
    call("d1", tool + "pending_decisions", {}, {"pending": []}, parent="a1")
    if os.environ.get("FAKE_CLAUDE_FOREIGN"):
        call("x1", "Bash", {"command": "ls"}, {}, parent="a1")
    call("t1", tool + "triage", {"log": log}, {
        "status": "decided", "label": labels.get(sid, "rtl"), "backend": "small",
        "confidence": 0.9, "low_confidence": False, "model": "haiku"})
else:
    unknown = {"answer": "I don't know", "citations": [], "unknown": True}
    call("c1", tool + "ask_check", unknown, {"ok": True, "answer": unknown})
emit({"type": "result", "subtype": "success", "num_turns": 3, "total_cost_usd": cost,
      "modelUsage": {"claude-haiku": {"outputTokens": 42}}})
"""


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the stand-in; returns the file it logs each call to (one JSON per line)."""
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir()
    script.write_text(FAKE_CLAUDE.replace("{python}", sys.executable))
    script.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("CHIPGRAPH_EVAL_CLAUDE", str(script))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(calls))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token-for-tests")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-for-tests")
    monkeypatch.setenv("VIRTUAL_ENV", "/nowhere")
    monkeypatch.delenv(runtimes.API_KEY_OPT_IN, raising=False)
    return calls


def _calls(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _opt(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _options(**kwargs: object) -> object:
    return evals.EvalOptions(runtime="claude-code", **kwargs)


def test_one_headless_claude_per_sample(
    tmp_path: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # log-05 is an rtl bug: the stand-in's `tb` for it is the one wrong answer.
    monkeypatch.setenv("FAKE_CLAUDE_LABELS", json.dumps({"log-05": "tb", "log-07": "tb"}))
    out = tmp_path / "out"
    options = _options(only=("log-05", "log-07"), main_model="sonnet", large_model="sonnet")
    result = evals.run_suite("triage", out, options)

    calls = _calls(fake_claude)
    assert len(calls) == 2
    argv = calls[0]["argv"]
    assert _opt(argv, "-p") == "/chipgraph:triage logs/log-05.log"
    assert _opt(argv, "--model") == "sonnet"
    assert _opt(argv, "--max-turns") == "30"
    assert _opt(argv, "--output-format") == "stream-json"
    assert "Agent" in _opt(argv, "--allowedTools").split()
    assert "Bash" in _opt(argv, "--disallowedTools").split()
    assert _opt(argv, "--max-budget-usd") == "3.00"
    # A fresh tinysoc copy with the decider tiers, and the dev plugin running this checkout.
    assert Path(calls[0]["cwd"]).name == "tinysoc"
    assert "    small: haiku\n    large: sonnet\n" in calls[0]["profile"]
    server = calls[0]["mcp"]["mcpServers"]["chipgraph"]
    assert server["command"] == "uv"
    assert server["args"][:3] == ["run", "--project", str(REPO)]
    assert server["args"][-1] == "mcp"
    # Auth: the token is passed on, the API key never; no VIRTUAL_ENV leaks into uv.
    assert all(c["token"] and not c["api_key"] and not c["venv"] for c in calls)

    summary = json.loads((out / "summary.json").read_text())
    assert summary["runtime"] == "claude-code"
    assert summary["auth"].startswith("oauth token")
    assert summary["models"] == {"main": "sonnet", "small": "haiku", "large": "sonnet"}
    assert summary["claude_code"] == "0.0.0 (fake Claude Code)"
    assert summary["cost_usd"] == {"budget": 3.0, "total": 1.0}
    assert (summary["metrics"]["correct"], summary["metrics"]["total"]) == (1, 2)
    assert result.status == "fail"  # 50 % < 80 %
    assert (out / "streams" / "log-05.jsonl").is_file()
    answers = [json.loads(line) for line in (out / "answers.jsonl").read_text().splitlines()]
    assert answers[0] == {
        "id": "log-05",
        "label": "tb",
        "backend": "small",
        "status": "decided",
        "rule": None,
        "confidence": 0.9,
        "low_confidence": False,
        "model": "haiku",
    }


def test_the_suite_stops_at_the_budget(
    tmp_path: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_COST", "1.25")
    ids = ("log-01", "log-02", "log-03", "log-04", "log-05")
    evals.run_suite("triage", tmp_path / "out", _options(only=ids, budget_usd=3.0))
    calls = _calls(fake_claude)
    # 1.25 + 1.25 + 1.25 reaches $3: the last two samples are not run.
    assert [_opt(c["argv"], "--max-budget-usd") for c in calls] == ["3.00", "1.75", "0.50"]
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert summary["samples"] == {"total": 5, "run": 3, "not_run": {"budget": ["log-04", "log-05"]}}
    assert summary["cost_usd"]["total"] == 3.75
    items = {i["id"]: i for i in summary["items"]}
    assert items["log-04"]["not_run"] == "budget" and not items["log-04"]["passed"]
    assert "not run, budget: log-04, log-05" in (tmp_path / "out" / "summary.md").read_text()


def test_the_suite_stops_at_the_time_limit(tmp_path: Path, fake_claude: Path) -> None:
    out = tmp_path / "out"
    evals.run_suite("triage", out, _options(only=("log-01", "log-02"), time_limit_s=0))
    assert not fake_claude.exists()
    summary = json.loads((out / "summary.json").read_text())
    assert summary["samples"]["not_run"] == {"time": ["log-01", "log-02"]}


def test_a_forbidden_tool_call_fails_the_run(
    tmp_path: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_FOREIGN", "1")
    monkeypatch.setenv("FAKE_CLAUDE_LABELS", json.dumps({"log-01": "rtl"}))
    result = evals.run_suite("triage", tmp_path / "out", _options(only=("log-01",)))
    assert result.status == "fail"
    assert result.summary["metrics"]["correct"] == 1
    assert result.summary["foreign_tool_calls"] == ["log-01:Bash"]


def test_the_ask_suite_reads_the_checked_answer(tmp_path: Path, fake_claude: Path) -> None:
    result = evals.run_suite("ask", tmp_path / "out", _options(only=("q16", "q01")))
    calls = _calls(fake_claude)
    assert [_opt(c["argv"], "-p") for c in calls] == [
        "/chipgraph:ask What is the reset value of the timer COMPARE register?",
        "/chipgraph:ask What is the baud rate of the tinysoc UART?",
    ]
    assert _opt(calls[0]["argv"], "--max-turns") == "12"
    assert "--disallowedTools" not in calls[0]["argv"]
    assert result.summary["models"] == {"main": "haiku", "asker": "haiku"}
    items = {i["id"]: i for i in result.summary["items"]}
    assert items["q16"]["passed"] and not items["q01"]["passed"]


def test_no_budget_flag_when_this_claude_has_none(tmp_path: Path) -> None:
    # `true --help` prints nothing: an older `claude` without --max-budget-usd.
    suite = evals.SUITES["triage"]
    runtime = runtimes.ClaudeCodeRuntime(
        suite,
        project_dir=tmp_path,
        plugin_dir=tmp_path / "plugin",
        streams_dir=tmp_path,
        main_model="haiku",
        spend=runtimes.Spend(3.0, 60.0),
        claude="true",
        env={"PATH": "/usr/bin:/bin"},
    )
    command = runtime.command(suite.items()[0])
    assert "--max-budget-usd" not in command and command[0] == "true"


def test_no_claude_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHIPGRAPH_EVAL_CLAUDE", raising=False)
    monkeypatch.setattr(runtimes.shutil, "which", lambda _: None)
    with pytest.raises(evals.EvalError, match="claude"):
        evals.run_suite("triage", tmp_path / "out", _options(only=("log-01",)))


# --- auth -------------------------------------------------------------------------------


def test_auth_prefers_the_token_and_never_passes_a_stray_api_key() -> None:
    env, auth = runtimes.claude_auth({"CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_API_KEY": "k"})
    assert auth.startswith("oauth token") and "ANTHROPIC_API_KEY" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "t"
    env, auth = runtimes.claude_auth({"ANTHROPIC_API_KEY": "k", "PATH": "/bin"})
    assert auth == "logged-in Claude Code account" and env == {"PATH": "/bin"}
    env, auth = runtimes.claude_auth({"ANTHROPIC_API_KEY": "k", runtimes.API_KEY_OPT_IN: "1"})
    assert auth.startswith("api key") and env["ANTHROPIC_API_KEY"] == "k"
    env, _ = runtimes.claude_auth({"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "VIRTUAL_ENV": "/v"})
    assert env == {}


# --- stream parsing ---------------------------------------------------------------------


def _check(parent: str | None, ok: bool, answer: str) -> dict[str, object]:
    return {
        "parent": parent,
        "name": "mcp__plugin_chipgraph_chipgraph__ask_check",
        "input": {"answer": answer, "citations": ["chip.yml:9"]},
        "result": {"ok": ok, "answer": {"answer": answer, "citations": ["chip.yml:9"]}},
    }


def test_the_ask_answer_is_the_main_sessions_accepted_check() -> None:
    calls = [_check("a1", True, "sub"), _check(None, False, "bad"), _check(None, True, "main")]
    answer = streams.ask_answer("q07", calls)
    assert (answer["answer"], answer["checked"]) == ("main", True)
    # Only the asker's own check: used, but unchecked, so it never passes.
    answer = streams.ask_answer("q07", calls[:1])
    assert (answer["answer"], answer["checked"]) == ("sub", False)
    assert streams.ask_answer("q07", [])["checked"] is False


def test_stream_cost_and_tokens(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(
        "not json\n"
        + json.dumps(
            {"type": "result", "total_cost_usd": 0.25, "modelUsage": {"m": {"outputTokens": 7}}}
        )
        + "\n"
    )
    events = streams.events(path)
    assert streams.cost_usd(events) == 0.25
    assert streams.output_tokens(events) == {"m": 7}
    assert streams.cost_usd([]) == 0.0
