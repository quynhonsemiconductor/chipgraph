"""The short summary of one suite run: `summary.json` and `summary.md`."""

from __future__ import annotations

from typing import Any

from .suites import Suite

SCHEMA_VERSION = 1
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")


def _per_class(confusion: dict[str, dict[str, int]]) -> dict[str, dict[str, Any]]:
    out = {}
    for label, row in confusion.items():
        total = sum(row.values())
        correct = row.get(label, 0)
        out[label] = {
            "total": total,
            "correct": correct,
            "accuracy": round(correct / total, 4) if total else None,
        }
    return out


def build_summary(
    suite: Suite,
    *,
    report: Any,
    runs: dict[str, dict[str, Any]],
    info: dict[str, Any],
) -> dict[str, Any]:
    """The summary of a graded run.

    `report` is the grader's `GradeReport` over every item (an item with no answer
    fails); `runs` the per-sample run records (`SampleRun` as a dict, by id); `info` the
    run's facts (runtime, models, versions, budget, wall time, Inspect log and metrics).
    """
    graded = report.to_json()
    items_key = "questions" if suite.kind == "ask" else "samples"
    grades = graded.pop(items_key)
    foreign = sorted(f for r in runs.values() for f in r.get("details", {}).get("foreign", []))
    not_run: dict[str, list[str]] = {}
    for sid, run in runs.items():
        if run.get("not_run"):
            not_run.setdefault(str(run["not_run"]), []).append(sid)
    passed = bool(report.passed) and not foreign
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "suite": suite.name,
        "description": suite.description,
        "status": "pass" if passed else "fail",
        "verdict": "PASS" if passed else "FAIL",
        **info,
        "samples": {
            "total": len(grades),
            "run": sum(1 for r in runs.values() if not r.get("not_run")),
            "not_run": not_run,
        },
        "cost_usd": {
            **info.get("cost_usd", {}),
            "total": round(sum(float(r.get("cost_usd") or 0.0) for r in runs.values()), 4),
        },
        "metrics": graded,
        "foreign_tool_calls": foreign,
        "items": [
            {
                **grade,
                "cost_usd": runs.get(grade["id"], {}).get("cost_usd", 0.0),
                "not_run": runs.get(grade["id"], {}).get("not_run"),
                "run_note": runs.get(grade["id"], {}).get("note", ""),
            }
            for grade in grades
        ],
    }
    if suite.kind == "triage":
        summary["per_class"] = _per_class(graded["confusion"])
    return summary


def no_data_summary(suite: Suite, info: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "suite": suite.name,
        "description": suite.description,
        "status": "no data",
        "verdict": "NO DATA",
        "reason": f"{suite.data.name} does not exist yet: nothing to evaluate",
        **info,
    }


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _author_lines(metrics: dict[str, Any]) -> list[str]:
    """The `rtl` / `tb` metrics against their thresholds."""
    th = metrics["thresholds"]
    lines = [
        f"- pass@1: {_pct(metrics['pass_at_1'])}; pass@k: {_pct(metrics['pass_at_k'])} "
        f"(needs >= {_pct(th['min_pass_at_k'])}); repair turns (tries used), mean: "
        f"{metrics['repair_turns_mean']}",
    ]
    if metrics["phase"] == "rtl":
        lines.append(
            f"- cycles matching the oracle, mean: {_pct(metrics.get('match_fraction_mean'))}"
        )
    else:
        lines.append(
            f"- soundness on the oracle, mean: {_pct(metrics.get('soundness_mean'))}; mutation "
            f"score, mean: {_pct(metrics.get('mutation_score_mean'))} "
            f"(needs >= {_pct(th['min_mutation_score'])})"
        )
    usage = metrics.get("usage_by_model") or {}
    per_model = ", ".join(
        f"{m} ${u.get('cost_usd', 0):.4f} / {int(sum(u.get(k, 0) for k in TOKEN_KEYS))} tok"
        for m, u in usage.items()
    )
    lines.append(f"- tokens: {metrics['tokens']}" + (f" ({per_model})" if per_model else ""))
    estimate = metrics.get("estimate")
    if estimate:
        lines.append(
            f"- estimated real run (fake run's text, {estimate['basis']}): "
            f"~${estimate['usd']:.2f} for {metrics['problems']} problem(s)"
        )
    return lines


def _author_table(metrics: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    """One row per problem."""
    rtl = metrics["phase"] == "rtl"
    match = "cycles match" if rtl else "sound / mutants"
    lines = [
        f"| problem | pass@1 | pass@k | tries | first rejection | {match} | cost | tokens | wall |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for g in items:
        if rtl:
            diff = g.get("diff") or {}
            got = f"{_pct(diff.get('match_fraction'))} ({diff.get('status', '-')})"
        else:
            tb = g.get("tb") or {}
            got = (
                f"{_pct(tb.get('soundness'))} / {len(tb.get('killed') or [])} of "
                f"{tb.get('mutants', 0)} killed"
            )
        lines.append(
            f"| {g['id']} | {'yes' if g['pass_at_1'] else 'no'} | "
            f"{'yes' if g['pass_at_k'] else 'no'} | {g['repair_turns']}/{g['k']} | "
            f"{g['first_rejection'] or '-'} | {got} | ${g['cost_usd']:.4f} | {g['tokens']} | "
            f"{g['wall_s']:.0f} s |"
        )
    return lines


def markdown(summary: dict[str, Any]) -> str:
    """`summary.md`: the verdict, the metrics against the thresholds, cost, and failures."""
    lines = [f"## chipgraph eval `{summary['suite']}`: {summary['verdict']}", ""]
    if summary["status"] == "no data":
        lines.append(f"{summary['reason']}.")
        return "\n".join(lines) + "\n"
    metrics = summary["metrics"]
    models = ", ".join(f"{k} `{v}`" for k, v in summary.get("models", {}).items())
    lines += [
        f"{summary['description']}.",
        "",
        f"- runtime: `{summary['runtime']}`" + (f" ({models})" if models else ""),
    ]
    if summary.get("auth"):
        lines.append(f"- auth: {summary['auth']}")
    if "correct_citation_rate" in metrics:
        th = metrics["thresholds"]
        lines += [
            f"- correct citations: {metrics['correct']}/{metrics['answerable']} "
            f"({_pct(metrics['correct_citation_rate'])}; needs >= {_pct(th['min_correct_rate'])})",
            f"- unanswerable: {metrics['said_unknown']}/{metrics['unanswerable']} said unknown; "
            f"invented: {metrics['invented']} (needs <= {th['max_invented']})",
        ]
    elif "pass_at_k" in metrics:
        lines += _author_lines(metrics)
    elif "recall" in metrics:
        th = metrics["thresholds"]
        by_class = ", ".join(
            f"{k} {v['caught']}/{v['total']}" for k, v in metrics["by_class"].items()
        )
        lines += [
            f"- planted defects caught: {metrics['caught']}/{metrics['defects']} "
            f"(recall {_pct(metrics['recall'])}; needs >= {_pct(th['min_recall'])})",
            f"- false alarms: {metrics['false_alarms']}/{metrics['clean']} clean diffs "
            f"(needs <= {th['max_false_alarms']}); precision proxy "
            f"{_pct(metrics['precision_proxy'])}",
            f"- by class: {by_class}",
        ]
    else:
        lines.append(
            f"- accuracy: {metrics['correct']}/{metrics['total']} "
            f"({_pct(metrics['accuracy'])}; needs >= {_pct(metrics['threshold'])})"
        )
        per_class = ", ".join(
            f"{label} {c['correct']}/{c['total']}"
            for label, c in summary["per_class"].items()
            if c["total"]
        )
        lines.append(f"- by class: {per_class}")
        backends = ", ".join(
            f"{name} {b['correct']}/{b['answered']}"
            for name, b in metrics["by_backend"].items()
            if b["answered"]
        )
        lines.append(f"- by backend: {backends or '-'}")
    samples = summary["samples"]
    not_run = "; ".join(f"{why}: {', '.join(ids)}" for why, ids in samples["not_run"].items())
    lines.append(
        f"- samples run: {samples['run']}/{samples['total']}"
        + (f" (not run, {not_run})" if not_run else "")
    )
    cost = summary["cost_usd"]
    budget = f" of a ${cost['budget']:.2f} budget" if cost.get("budget") is not None else ""
    lines.append(f"- cost: ${cost['total']:.4f}{budget}; wall time {summary['wall_s']:.0f} s")
    if summary["foreign_tool_calls"]:
        lines.append(f"- forbidden tool calls: {', '.join(summary['foreign_tool_calls'])}")
    if "pass_at_k" in metrics:
        lines += ["", *_author_table(metrics, summary["items"])]
    failed = [i for i in summary["items"] if not i["passed"]]
    if failed:
        lines += ["", "| sample | note |", "|---|---|"]
        lines += [f"| {i['id']} | {i.get('note') or i.get('run_note') or '-'} |" for i in failed]
    return "\n".join(lines) + "\n"


__all__ = ["build_summary", "markdown", "no_data_summary"]
