"""`PrGateTool`: the `pr_gate` tool of rule `digital-rtl/pr_gate` (M2-11a).

The rule's input is `pr/<block>.json` (what `open_pr` wrote) and its gate `pr:<block>`,
so the scheduler runs this only once the gate is approved:

- with `review: {use: github}`, by a reviewer's approval on the pull request at its
  current head commit (`GitHubGateChecker`; a later push makes it stale);
- with the `file` adapter, by `chipgraph approve pr:<block>`, which pins the hashes of
  the rule's inputs (the PR record, the block's files): offline.

With the GitHub adapter it first compares the head commit of `pr/<block>.json` with the
pull request's **live** head: if someone pushed to the branch since `open_pr` ran, the
record is stale and the gate does not pass (it says to run the target again, which fast-forwards
the record) instead of recording a commit nobody reviewed.

It writes `pr/<block>.approved.json` (default; arg `out`): the branch and head commit
that were approved. It never merges: merging stays a person's act on the platform.
"""

from __future__ import annotations

import json
import time

from chipgraph.adapters.review.github import ReviewError
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.checks._common import compute_idempotency_key
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.plugin_api.types import ToolContext
from chipgraph.packs.digital_rtl.pr.open import DEFAULT_OUT, OpenPrTool, read_record

DEFAULT_APPROVED = "pr/{block}.approved.json"


class PrGateTool:
    """Records the approved head of a block's pull request, once its gate is approved."""

    name = "pr_gate"
    capability = "gen"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        block = ctx.params.get("block", "")
        record_t = spec.args.get("pr", DEFAULT_OUT)
        out_t = spec.args.get("out", DEFAULT_APPROVED)
        if not block or not isinstance(record_t, str) or not isinstance(out_t, str):
            return self._result(spec, "error", "pr_gate needs a block and string templates", start)
        rel = record_t.replace("{block}", block)
        record = read_record(ctx.repo_root / rel)
        if record is None:
            return self._result(spec, "fail", f"no valid PR record at {rel}", start)
        if record.review == "github":
            stale = self._stale(ctx, record.number, record.head_sha, block)
            if stale is not None:
                return self._result(spec, "fail", stale, start)
        out = ctx.repo_root / out_t.replace("{block}", block)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "block": block,
            "gate": record.gate,
            "branch": record.branch,
            "head_sha": record.head_sha,
            "number": record.number,
            "url": record.url,
        }
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return self._result(spec, "pass", f"{record.gate} approved at {record.head_sha}", start)

    @staticmethod
    def _stale(ctx: ToolContext, number: int | None, recorded: str, block: str) -> str | None:
        """Why the record cannot be trusted (None when the PR's live head is the recorded one)."""
        if number is None:
            return f"the PR record of {block!r} has no pull request number"
        try:
            review = OpenPrTool.review_for(AppContext.load(ctx.repo_root))
            if review is None:
                return "the PR record says github, but the profile has no github review adapter"
            live = review.pull_request(number)
        except (AppError, ReviewError, OSError) as exc:
            return f"cannot read pull request #{number}: {exc}"
        if live.state != "open":
            return f"pull request #{number} is {live.state}, not open"
        if live.head_sha != recorded:
            return (
                f"pull request #{number} head is {live.head_sha[:12]} but the record has "
                f"{recorded[:12]}: the branch moved after open_pr ran; run the target again "
                "so the record, and the review, follow the live head"
            )
        return None

    @staticmethod
    def _result(spec: CheckSpec, status: str, message: str, start: float) -> CheckResult:
        return CheckResult(
            check_id=spec.id,
            status=status,  # type: ignore[arg-type]
            issues=(
                Issue(
                    rule="pr_gate",
                    severity="info" if status == "pass" else "error",
                    msg=message,
                ),
            ),
            duration_s=time.monotonic() - start,
            idempotency_key=compute_idempotency_key(spec.id, spec.args, ()),
        )


__all__ = ["DEFAULT_APPROVED", "PrGateTool"]
