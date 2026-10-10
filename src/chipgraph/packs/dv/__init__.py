"""The ``dv`` pack: design verification (DESIGN.md 8.2), task M2-06.

- ``rules/tb_module.yml``: the rule ``dv/tb_module``, a cocotb test per block written by
  the ``tb-author`` role from the spec and the interface, never the RTL;
- ``skills/dv/cocotb.md``: the skill ``dv/cocotb`` (for ``tb-author`` only);
- :mod:`chipgraph.packs.dv.interface`: the interface a test is written against
  (``interface_for``: spec ports, else the Design Model's RTL ports, else the module's
  declaration) and the block's spec slice;
- :mod:`chipgraph.packs.dv.declaration`: a module's declaration read with pyslang from
  the syntax tree's module header only.

The pack directory is discovered by :func:`chipgraph.app.build.builtin_packs_dir`; a
project turns it on with ``packs: [dv]`` and configures the ``tb_static`` check
(``adapters.tb_static: {use: tb_static}``).
"""
