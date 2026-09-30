"""The ``assist`` pack: whole-project assistance commands (DECISIONS D36).

This pack ships ``/audit`` (task M1-20): it runs every deterministic check the project's
profile configures over the whole project, records their findings in the finding store as
usual, then builds a report that groups the findings by DESIGN.md 4.8 layer and severity
(:mod:`chipgraph.packs.assist.audit`). The report is what ``chipgraph audit`` prints and
the ``audit`` MCP tool returns; ``chipgraph try`` (task M1-21) reuses :func:`run_audit`.

The pack carries no code adapters of its own -- it orchestrates the checks other packs
register -- so, like every built-in pack, its directory is discovered by
:func:`chipgraph.app.build.builtin_packs_dir`.
"""

from chipgraph.packs.assist.audit import AuditReport, run_audit

__all__ = ["AuditReport", "run_audit"]
