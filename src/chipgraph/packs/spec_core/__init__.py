"""The ``spec-core`` pack: deterministic extractors for text specifications.

This pack ships the ``mas-markdown`` extractor (:mod:`chipgraph.packs.spec_core.extract.mas`),
which reads QNSC-style Micro-Architecture Specification (MAS) Markdown into the Design
Model: requirements (declared or inferred, D37), interface ports, register maps with
fields, and open items. The extractor is registered through the
``chipgraph.adapters.extractor`` entry point; the pack directory itself is discovered by
:func:`chipgraph.app.build.builtin_packs_dir`.
"""
