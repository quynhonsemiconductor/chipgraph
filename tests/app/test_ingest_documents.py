"""M1-13: `chipgraph ingest` indexes the `/ask` documents, and keeps them on re-ingest.

`ModelStore.write()` rebuilds the database and drops every document, so ingest must index
them again right after it writes the model. These tests run ingest on a tinysoc copy.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from chipgraph.app.context import AppContext
from chipgraph.app.ingest import run_ingest
from chipgraph.cli import app
from chipgraph.core.model.store import ModelStore, default_model_db_path

_EXAMPLE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "tinysoc"

_TINYSOC_DOCUMENTS = (
    "README.md",
    "chip.yml",
    "doc/specs/TINY_GPIO_MAS.md",
    "doc/specs/TINY_TIMER_MAS.md",
    "filelists/gpio.f",
    "filelists/timer.f",
    "filelists/top.f",
    "rtl/tiny_gpio.sv",
    "rtl/tiny_timer.sv",
    "rtl/tiny_top.sv",
)


def _tinysoc(dest: Path, profile_extra: str = "") -> Path:
    shutil.copytree(_EXAMPLE_ROOT, dest)
    if profile_extra:
        profile = dest / ".chipgraph.yml"
        profile.write_text(profile.read_text() + "\n" + profile_extra)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=dest, check=True)
    return dest


def _doc_paths(root: Path, query: str) -> set[str]:
    hits = ModelStore(default_model_db_path(root)).search(query, limit=200)
    return {hit.file for hit in hits if hit.file is not None}


def test_ingest_indexes_inputs_and_markdown_docs(tmp_path: Path) -> None:
    root = _tinysoc(tmp_path / "t")
    report = run_ingest(AppContext.load(root))
    assert report.documents is not None
    assert report.documents.indexed == _TINYSOC_DOCUMENTS  # deterministic, sorted
    assert report.documents.lines > 0
    assert report.documents.skipped == ()
    # Searchable, citing file:line.
    [hit] = [
        h
        for h in ModelStore(report.db_path).search('"REQ-TIM-004"', limit=50)
        if h.file == "doc/specs/TINY_TIMER_MAS.md" and h.line == 78
    ]
    assert hit.citation == "doc/specs/TINY_TIMER_MAS.md:78"
    assert "IRQ_CLR" in hit.text


def test_a_second_ingest_keeps_documents_searchable(tmp_path: Path) -> None:
    root = _tinysoc(tmp_path / "t")
    ctx = AppContext.load(root)
    first = run_ingest(ctx)
    before = _doc_paths(root, '"tinysoc"')
    second = run_ingest(AppContext.load(root))
    after = _doc_paths(root, '"tinysoc"')
    assert before and before == after
    assert first.documents == second.documents
    assert "doc/specs/TINY_GPIO_MAS.md" in _doc_paths(root, '"DATA_OUT"')


def test_nda_files_are_not_indexed_and_the_report_says_so(tmp_path: Path) -> None:
    root = _tinysoc(
        tmp_path / "t", 'data:\n  nda_paths: ["doc/specs/TINY_GPIO_MAS.md", "rtl/tiny_gpio.sv"]\n'
    )
    report = run_ingest(AppContext.load(root))
    assert report.documents is not None
    assert report.documents.nda_skipped == ("doc/specs/TINY_GPIO_MAS.md", "rtl/tiny_gpio.sv")
    assert "doc/specs/TINY_GPIO_MAS.md" not in report.documents.indexed
    assert "rtl/tiny_gpio.sv" not in report.documents.indexed
    # The model itself still has the block's content (ingest reads it); only /ask skips it.
    assert report.model.get("register:gpio.DIR") is not None
    assert _doc_paths(root, '"DATA_OUT" OR "pin_out"').isdisjoint(
        {"doc/specs/TINY_GPIO_MAS.md", "rtl/tiny_gpio.sv"}
    )
    issues = [i for i in report.issues if i.code == "ask.nda_not_indexed"]
    assert [i.file for i in issues] == ["doc/specs/TINY_GPIO_MAS.md", "rtl/tiny_gpio.sv"]
    assert all(i.severity == "info" for i in issues)


def test_vendor_markdown_is_not_indexed(tmp_path: Path) -> None:
    root = _tinysoc(tmp_path / "t")
    (root / "doc" / "vendor").mkdir()
    (root / "doc" / "vendor" / "UPSTREAM.md").write_text("# upstream zebracorn notes\n")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("# zebracorn guide\n")
    report = run_ingest(AppContext.load(root))
    assert report.documents is not None
    assert "docs/guide.md" in report.documents.indexed
    skipped = {s.path: s.reason for s in report.documents.skipped}
    assert skipped == {"doc/vendor/UPSTREAM.md": "vendor"}
    assert _doc_paths(root, '"zebracorn"') == {"docs/guide.md"}


def test_ingest_cli_prints_the_document_count(tmp_path: Path) -> None:
    root = _tinysoc(tmp_path / "t")
    result = CliRunner().invoke(app, ["-C", str(root), "ingest"])
    assert result.exit_code == 0, result.output
    assert "documents indexed for /ask: 10 (" in result.output
    assert "0 nda file(s) not indexed" in result.output
