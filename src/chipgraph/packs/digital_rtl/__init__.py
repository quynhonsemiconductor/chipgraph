"""The ``digital-rtl`` pack: rules and skills for digital RTL (DESIGN.md 8.2).

- the Critic review of each block's change: the rule ``digital-rtl/review``
  (``rules/review.yml``) and its skill ``review/diff`` (``skills/review-diff.md``);
- the Planner (M2-03): the rule ``digital-rtl/plan`` (``rules/plan.yml``, skill
  ``plan/modules``, check ``plan_check``), the gate ``plan:{block}`` on the rule
  ``digital-rtl/plan_expand`` (``rules/plan_expand.yml``), and the code behind them in
  :mod:`chipgraph.packs.digital_rtl.plan` (the plan file model, the ``plan.modules``
  resolver, the ``plan_expand`` tool, ``chipgraph plan show`` and the planner context).

The pack directory is discovered by :func:`chipgraph.app.build.builtin_packs_dir`; a
project turns it on with ``packs: [digital-rtl]``.
"""
