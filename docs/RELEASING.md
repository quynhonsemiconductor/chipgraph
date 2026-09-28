# Releasing chipgraph

`chipgraph` publishes to PyPI (and TestPyPI, for dry runs) via
[`.github/workflows/release.yml`](../.github/workflows/release.yml), using PyPI
[trusted publishing](https://docs.pypi.org/trusted-publishers/) (OIDC — no API tokens
stored anywhere).

## One-time setup (the lead does this once)

1. **PyPI**: create the pending publisher for a project named `chipgraph` at
   <https://pypi.org/manage/account/publishing/> — "Add a new pending publisher":
   - Repository owner: `quynhonsemiconductor`
   - Repository name: `chipgraph`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
2. **TestPyPI**: same at <https://test.pypi.org/manage/account/publishing/>, with
   environment name `testpypi`.
3. **GitHub environments**: in the repo's Settings → Environments, create:
   - `pypi` — add required reviewers (the lead, at minimum) so a push to `pypi` needs
     manual approval.
   - `testpypi` — no required reviewers needed (safe to auto-run).

   The environment names must match exactly what the workflow uses
   (`environment: pypi` / `environment: testpypi`) and what was entered as the trusted
   publisher's "Environment name" above — PyPI's OIDC check matches on that name.
4. First publish only: PyPI/TestPyPI create the actual project the first time a
   trusted-publisher release succeeds. Until then the "pending publisher" holds the
   name `chipgraph` for this repo/workflow so nobody else can register it first.

## Cutting a release

1. Bump the version in **two places** and keep them equal:
   - `pyproject.toml` → `[project].version`
   - `plugin/.claude-plugin/plugin.json` → `version`
   - `plugin/.mcp.json` → the `chipgraph@X.Y.Z` pin in the server's `args`

   `tests/test_release_metadata.py` fails CI if these drift apart.
2. Commit the bump, merge to `main`.
3. Tag and push:
   - `git tag vX.Y.Z && git push origin vX.Y.Z` → the `build` job runs, then
     `publish-pypi` (requires the `pypi` environment's reviewer approval).
   - `git tag vX.Y.ZrcN && git push origin vX.Y.ZrcN` (a release-candidate tag
     containing `rc`) → `build` then `publish-testpypi`, no approval needed.
   - Or run the workflow manually (Actions → Release → Run workflow) and pick
     `testpypi` or `pypi` as the target — useful to republish without a new tag.
4. The `build` job fails the whole run if the pushed tag's version (`vX.Y.Z` →
   `X.Y.Z`) does not match `pyproject.toml`'s `version`, so a stale bump can't slip
   through.

## Verifying a release

Against TestPyPI (falls back to real PyPI for dependencies that aren't mirrored
there):

```
uvx --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ chipgraph@X.Y.Z --version
```

Against real PyPI:

```
uvx chipgraph@X.Y.Z --version
```

Then exercise the plugin: `/plugin marketplace add quynhonsemiconductor/chipgraph`,
`/plugin install chipgraph@chipgraph`, and confirm the `chipgraph` MCP server starts
(the `/mcp` panel in Claude Code, or `claude plugin details chipgraph`).
