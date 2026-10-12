"""Security review of M2-12 (findings 9, 11, 12, 17): `vendor_guard` fails closed on links,
unreadable entries, a manifest outside the repository, nested `.git` directories and
case-folded duplicates, and never quotes the manifest's lines."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import FakeRunner, make_ctx, write

from chipgraph.checks.vendor_guard import (
    ManifestError,
    VendorGuardCheck,
    parse_manifest,
    write_manifest,
)
from chipgraph.core.contracts import CheckResult, CheckSpec

TINYSOC = Path(__file__).resolve().parents[2] / "examples" / "tinysoc"
ROOT_USER = hasattr(os, "geteuid") and os.geteuid() == 0
needs_permissions = pytest.mark.skipif(
    ROOT_USER or os.name != "posix", reason="needs POSIX permissions that bind (not root)"
)


def _run(root: Path, **args: object) -> CheckResult:
    spec = CheckSpec(
        id="vendor_guard", capability="vendor_guard", adapter="vendor_guard", args=args
    )
    return asyncio.run(VendorGuardCheck().run(spec, make_ctx(root, FakeRunner())))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _vendor(root: Path, files: dict[str, str], *, extra: str = "") -> None:
    lines = ["# recorded"]
    for rel, text in files.items():
        write(root, f"vendor/{rel}", text)
        lines.append(f"{_sha(text)}  {rel}")
    write(root, "vendor/MANIFEST.sha256", "\n".join(lines) + "\n" + extra)


def _rules(result: CheckResult) -> list[str]:
    return [i.rule for i in result.issues]


@pytest.fixture
def restore_modes(tmp_path: Path) -> Iterator[None]:
    yield
    for dirpath, dirnames, filenames in os.walk(tmp_path):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            if not path.is_symlink():
                path.chmod(0o755)


# --- finding 9: symbolic links ----------------------------------------------------------------


def test_a_symlinked_directory_is_reported(tmp_path: Path) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n"})
    write(tmp_path, "rtl/top.sv", "module top; endmodule\n")
    (tmp_path / "vendor/extra").symlink_to("../rtl")
    result = _run(tmp_path)
    assert result.status == "fail"
    assert [(i.rule, i.file) for i in result.issues] == [("vendor_guard.symlink", "vendor/extra")]


def test_a_listed_directory_replaced_by_a_link_fails_and_its_new_file_is_never_trusted(
    tmp_path: Path,
) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n"})
    shutil.rmtree(tmp_path / "vendor/ip")
    write(tmp_path, "elsewhere/a.sv", "x\n")  # matches the recorded hash
    write(tmp_path, "elsewhere/x.sv", "module evil; endmodule\n")
    (tmp_path / "vendor/ip").symlink_to("../elsewhere")
    result = _run(tmp_path)
    assert result.status == "fail"
    assert ("vendor_guard.symlink", "vendor/ip") in [(i.rule, i.file) for i in result.issues]


def test_a_listed_file_replaced_by_a_link_to_identical_content_fails(tmp_path: Path) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n"})
    write(tmp_path, "outside/a.sv", "x\n")
    (tmp_path / "vendor/ip/a.sv").unlink()
    (tmp_path / "vendor/ip/a.sv").symlink_to("../../outside/a.sv")
    result = _run(tmp_path)
    assert result.status == "fail"
    assert [(i.rule, i.file) for i in result.issues] == [("vendor_guard.symlink", "vendor/ip/a.sv")]


def test_a_dangling_link_fails(tmp_path: Path) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n"})
    (tmp_path / "vendor/ip/a.sv").unlink()
    (tmp_path / "vendor/ip/a.sv").symlink_to("nowhere.sv")
    assert _run(tmp_path).status == "fail"


def test_a_symlinked_manifest_is_an_error(tmp_path: Path) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n"})
    manifest = tmp_path / "vendor/MANIFEST.sha256"
    (tmp_path / "real.sha256").write_text(manifest.read_text())
    manifest.unlink()
    manifest.symlink_to("../real.sha256")
    result = _run(tmp_path)
    assert result.status == "error" and _rules(result) == ["vendor_guard.symlink"]


def test_a_symlinked_vendor_directory_is_an_error(tmp_path: Path) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n"})
    (tmp_path / "vendor").rename(tmp_path / "real_vendor")
    (tmp_path / "vendor").symlink_to("real_vendor")
    result = _run(tmp_path)
    assert result.status == "error" and result.issues[0].file == "vendor"


def test_write_manifest_refuses_a_tree_with_a_link(tmp_path: Path) -> None:
    write(tmp_path, "v/a.sv", "x\n")
    (tmp_path / "v/b.sv").symlink_to("a.sv")
    with pytest.raises(ManifestError, match="links"):
        write_manifest(tmp_path / "v")


# --- finding 11: the manifest arg -------------------------------------------------------------


@pytest.mark.parametrize("manifest", ["/etc/hosts", "../MANIFEST.sha256", "vendor/../../x", "C:/x"])
def test_a_manifest_outside_the_repository_is_refused(tmp_path: Path, manifest: str) -> None:
    result = _run(tmp_path / "repo", manifest=manifest)
    assert result.status == "error" and "repo-relative" in result.issues[0].msg


def test_a_parse_error_never_quotes_the_line(tmp_path: Path) -> None:
    write(tmp_path, "vendor/MANIFEST.sha256", "SECRET-TOKEN-1234 is not a manifest line\n")
    result = _run(tmp_path)
    assert result.status == "error"
    assert "SECRET-TOKEN" not in result.issues[0].msg and ":1:" in result.issues[0].msg


def test_a_non_utf8_manifest_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor/MANIFEST.sha256").write_bytes(b"\xff\xfe\n")
    result = _run(tmp_path)
    assert result.status == "error" and "not UTF-8" in result.issues[0].msg


# --- finding 12: unreadable entries -----------------------------------------------------------


@needs_permissions
def test_an_unreadable_listed_file_is_an_error_not_a_crash(
    tmp_path: Path, restore_modes: None
) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n"})
    (tmp_path / "vendor/ip/a.sv").chmod(0)
    result = _run(tmp_path)
    assert result.status == "error"
    assert [(i.rule, i.file) for i in result.issues] == [
        ("vendor_guard.unreadable", "vendor/ip/a.sv")
    ]


@needs_permissions
def test_an_unreadable_directory_is_an_error_not_a_pass(
    tmp_path: Path, restore_modes: None
) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n"})
    write(tmp_path, "vendor/hidden/x.sv", "module evil; endmodule\n")
    (tmp_path / "vendor/hidden").chmod(0)
    result = _run(tmp_path)
    assert result.status == "error"
    assert ("vendor_guard.unreadable", "vendor/hidden") in [(i.rule, i.file) for i in result.issues]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_in_place_of_a_listed_file_fails_without_hanging(tmp_path: Path) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n"})
    (tmp_path / "vendor/ip/a.sv").unlink()
    os.mkfifo(tmp_path / "vendor/ip/a.sv")
    result = _run(tmp_path)
    assert result.status == "fail" and _rules(result) == ["vendor_guard.missing"]


# --- finding 17: nested .git, the idempotency key, case-folded duplicates ----------------------


def test_only_the_top_git_directory_is_skipped(tmp_path: Path) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n"})
    write(tmp_path, "vendor/.git/config", "[core]\n")
    assert _run(tmp_path).status == "pass"
    write(tmp_path, "vendor/ip/.git/evil.sv", "module evil; endmodule\n")
    result = _run(tmp_path)
    assert result.status == "fail"
    assert [(i.rule, i.file) for i in result.issues] == [
        ("vendor_guard.unlisted", "vendor/ip/.git/evil.sv")
    ]


def test_the_idempotency_key_follows_the_unlisted_files(tmp_path: Path) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n"})
    write(tmp_path, "vendor/ip/one.sv", "1\n")
    first = _run(tmp_path).idempotency_key
    (tmp_path / "vendor/ip/one.sv").rename(tmp_path / "vendor/ip/two.sv")
    assert _run(tmp_path).idempotency_key != first


def test_entries_that_differ_only_in_case_are_refused(tmp_path: Path) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n"}, extra=f"{_sha('x\n')}  IP/A.sv\n")
    result = _run(tmp_path)
    assert result.status == "error" and "differ only in case" in result.issues[0].msg


# --- what the reviewers tried that held: kept as tests ----------------------------------------


@pytest.mark.parametrize(
    "rel", ["../x", "a/../../x", "/etc/passwd", "./../x", "ip/./../../x", " ../x", ".."]
)
def test_entries_that_leave_the_directory_are_refused(rel: str) -> None:
    with pytest.raises(ManifestError, match="leaves the manifest's directory"):
        parse_manifest(f"{'a' * 64}  {rel}\n", "MANIFEST.sha256")


def test_a_nul_in_an_entry_is_refused() -> None:
    with pytest.raises(ManifestError, match="NUL"):
        parse_manifest(f"{'a' * 64}  ip/a\x00.sv\n", "MANIFEST.sha256")


def test_a_manifest_listing_itself_with_a_wrong_hash_fails(tmp_path: Path) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n"}, extra=f"{'0' * 64}  MANIFEST.sha256\n")
    result = _run(tmp_path)
    assert result.status == "fail" and _rules(result) == ["vendor_guard.modified"]


def test_tinysoc_still_passes(tmp_path: Path) -> None:
    root = tmp_path / "t"
    shutil.copytree(TINYSOC, root)
    result = _run(root)
    assert result.status == "pass", result.issues
