"""The pull request of a block's planned RTL (M2-11a): its branch, its body, its gate.

- `open`: the `open_pr` tool of rule `digital-rtl/open_pr`: builds the branch
  `chipgraph/<run id>/<block>` from the block's RTL and testbench files without touching
  the user's checkout, pushes it, opens the PR and writes `pr/<block>.json`;
- `gate`: the `pr_gate` tool of rule `digital-rtl/pr_gate`, gated by `pr:<block>`;
- `body`: the PR body: the plan, requirement coverage, check results and the Critic's
  verdict, factual and short.

Nothing here merges a pull request, or asks GitHub to.
"""
