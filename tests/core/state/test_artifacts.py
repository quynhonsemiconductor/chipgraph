"""Tests for hashing, data labels, and the file-based artifact store."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from chipgraph.core.contracts import ArtifactRef
from chipgraph.core.state.artifacts import (
    ArtifactStore,
    LabelRules,
    hash_bytes,
    hash_file,
    hash_inputs,
)


def test_hash_bytes_stable() -> None:
    assert hash_bytes(b"hello") == hash_bytes(b"hello")
    assert hash_bytes(b"hello") != hash_bytes(b"world")
    assert len(hash_bytes(b"hello")) == 64


def test_hash_file_matches_hash_bytes(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"some content" * 1000)
    assert hash_file(path) == hash_bytes(b"some content" * 1000)


def test_hash_inputs_order_independent() -> None:
    a = hash_inputs([(".:a.sv", "h1"), (".:b.sv", "h2")])
    b = hash_inputs([(".:b.sv", "h2"), (".:a.sv", "h1")])
    assert a == b


def test_hash_inputs_sensitive_to_content() -> None:
    a = hash_inputs([(".:a.sv", "h1")])
    b = hash_inputs([(".:a.sv", "h2")])
    assert a != b


def test_hash_inputs_stable_across_calls() -> None:
    pairs = [(".:a.sv", "h1"), (".:b.sv", "h2")]
    assert hash_inputs(pairs) == hash_inputs(pairs)


class TestLabelRules:
    def test_default_label(self) -> None:
        rules = LabelRules()
        assert rules.label_for("design/timer/rtl/m_x.sv") == "internal"

    def test_custom_default(self) -> None:
        rules = LabelRules(default="public")
        assert rules.label_for("anything.sv") == "public"

    def test_specific_glob_wins(self) -> None:
        rules = LabelRules(
            {
                "**/*.sv": "public",
                "design/nda/**": "nda",
            }
        )
        assert rules.label_for("design/rtl/m_x.sv") == "public"
        assert rules.label_for("design/nda/secret.sv") == "nda"

    def test_double_star_spans_directories(self) -> None:
        rules = LabelRules({"doc/nda/**": "nda"})
        assert rules.label_for("doc/nda/deep/nested/file.md") == "nda"
        assert rules.label_for("doc/other/file.md") == "internal"

    def test_longer_pattern_wins_on_tie_length_ordering(self) -> None:
        # Both match; the longer (more specific) pattern string wins regardless of order.
        rules = LabelRules(
            {
                "design/nda/**": "nda",
                "**": "public",
            }
        )
        assert rules.label_for("design/nda/x.sv") == "nda"

    def test_later_entry_wins_ties(self) -> None:
        # Two distinct 11-character patterns that both match "design/x.sv"; the
        # later entry (nda) must win the length tie.
        rules = LabelRules(
            {
                "design/*.sv": "public",
                "*esign/x.sv": "nda",
            }
        )
        assert rules.label_for("design/x.sv") == "nda"


class TestArtifactStore:
    def _store(self, root: Path, labels: LabelRules | None = None) -> ArtifactStore:
        return ArtifactStore(root=root, labels=labels or LabelRules(), repo="myrepo")

    def test_ref_applies_label(self, tmp_path: Path) -> None:
        labels = LabelRules({"nda/**": "nda"})
        store = self._store(tmp_path, labels)
        ref = store.ref("nda/secret.sv", "rtl")
        assert ref.label == "nda"
        assert ref.repo == "myrepo"
        assert ref.path == "nda/secret.sv"

    def test_artifact_hashes_file(self, tmp_path: Path) -> None:
        (tmp_path / "a.sv").write_text("module a; endmodule\n")
        store = self._store(tmp_path)
        artifact = store.artifact("a.sv", "rtl")
        assert artifact.content_hash == hash_file(tmp_path / "a.sv")
        assert artifact.ref.path == "a.sv"
        assert artifact.produced_by is None
        assert artifact.inputs_hash is None

    def test_artifact_missing_file_raises(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        with pytest.raises(FileNotFoundError):
            store.artifact("missing.sv", "rtl")

    def test_artifact_model_key_rejected(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        with pytest.raises(ValueError, match="Design Model"):
            store.exists(ArtifactRef(kind="model", model_key="block/timer"))

    def test_exists(self, tmp_path: Path) -> None:
        (tmp_path / "a.sv").write_text("x")
        store = self._store(tmp_path)
        assert store.exists(store.ref("a.sv", "rtl")) is True
        assert store.exists(store.ref("missing.sv", "rtl")) is False

    def test_current_hashes_skips_missing(self, tmp_path: Path) -> None:
        (tmp_path / "a.sv").write_text("aaa")
        store = self._store(tmp_path)
        refs = [store.ref("a.sv", "rtl"), store.ref("missing.sv", "rtl")]
        hashes = store.current_hashes(refs)
        assert set(hashes) == {"myrepo:a.sv"}
        assert hashes["myrepo:a.sv"] == hash_file(tmp_path / "a.sv")

    def test_invalid_path_rejected_via_contract(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        with pytest.raises(ValidationError):
            store.ref("../escape.sv", "rtl")
