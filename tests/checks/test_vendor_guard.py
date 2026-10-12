"""M2-12: `vendor_guard`, the equivalent of QSoC's `make vendor-guard`: vendored IP is never
edited in place. A manifest in `sha256sum` format records every file; the check fails when
a file differs, is gone or is not recorded."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import FakeRunner, make_ctx, write

from chipgraph.checks import vendor_guard
from chipgraph.checks.vendor_guard import (
    ManifestError,
    VendorGuardCheck,
    read_manifest,
    write_manifest,
)
from chipgraph.core.contracts import CheckResult, CheckSpec
from chipgraph.core.plugin_api.protocols import Check

TINYSOC = Path(__file__).resolve().parents[2] / "examples" / "tinysoc"


def _run(root: Path, runner: FakeRunner, **args: object) -> CheckResult:
    spec = CheckSpec(
        id="vendor_guard", capability="vendor_guard", adapter="vendor_guard", args=args
    )
    return asyncio.run(VendorGuardCheck().run(spec, make_ctx(root, runner)))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _vendor(root: Path, files: dict[str, str], *, manifest: str = "vendor/MANIFEST.sha256") -> None:
    lines = ["# recorded"]
    for rel, text in files.items():
        write(root, f"vendor/{rel}", text)
        lines.append(f"{_sha(text)}  {rel}")
    write(root, manifest, "\n".join(lines) + "\n")


def _rules(result: CheckResult) -> list[str]:
    return [i.rule for i in result.issues]


def test_is_a_check_and_chip_wide() -> None:
    check = VendorGuardCheck()
    assert isinstance(check, Check)
    assert (check.id, check.per_block) == ("vendor_guard", False)


def test_unchanged_files_pass(tmp_path: Path, runner: FakeRunner) -> None:
    _vendor(tmp_path, {"ip/a.sv": "module a; endmodule\n", "ip/b.f": "a.sv\n"})
    result = _run(tmp_path, runner)
    assert result.status == "pass" and result.issues == ()


def test_a_modified_file_fails_naming_it(tmp_path: Path, runner: FakeRunner) -> None:
    _vendor(tmp_path, {"ip/a.sv": "module a; endmodule\n"})
    (tmp_path / "vendor/ip/a.sv").write_text("module a; /* patched */ endmodule\n")
    result = _run(tmp_path, runner)
    assert result.status == "fail"
    [issue] = result.issues
    assert (issue.rule, issue.file) == ("vendor_guard.modified", "vendor/ip/a.sv")
    assert "never edited in place" in issue.msg and "vendor/MANIFEST.sha256" in issue.msg


def test_a_deleted_file_fails(tmp_path: Path, runner: FakeRunner) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n", "ip/b.sv": "y\n"})
    (tmp_path / "vendor/ip/b.sv").unlink()
    result = _run(tmp_path, runner)
    assert result.status == "fail" and _rules(result) == ["vendor_guard.missing"]
    assert result.issues[0].file == "vendor/ip/b.sv"


def test_a_file_the_manifest_does_not_list_fails(tmp_path: Path, runner: FakeRunner) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n"})
    write(tmp_path, "vendor/ip/extra.sv", "module extra; endmodule\n")
    result = _run(tmp_path, runner)
    assert result.status == "fail" and _rules(result) == ["vendor_guard.unlisted"]
    assert "record it, or remove it" in result.issues[0].msg


def test_allowed_globs_are_not_unlisted(tmp_path: Path, runner: FakeRunner) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n"})
    write(tmp_path, "vendor/patches/0001.patch", "reviewed change\n")
    assert _run(tmp_path, runner).status == "fail"
    assert _run(tmp_path, runner, allow=["patches/**"]).status == "pass"


def test_a_manifest_elsewhere_guards_its_own_directory(tmp_path: Path, runner: FakeRunner) -> None:
    _vendor(tmp_path, {"a.sv": "x\n"}, manifest="third_party/MANIFEST.sha256")
    write(tmp_path, "third_party/a.sv", "x\n")
    shutil.rmtree(tmp_path / "vendor")
    assert _run(tmp_path, runner, manifest="third_party/MANIFEST.sha256").status == "pass"
    (tmp_path / "third_party/a.sv").write_text("y\n")
    [issue] = _run(tmp_path, runner, manifest="third_party/MANIFEST.sha256").issues
    assert issue.file == "third_party/a.sv"


def test_no_manifest_is_an_error(tmp_path: Path, runner: FakeRunner) -> None:
    result = _run(tmp_path, runner)
    assert result.status == "error" and _rules(result) == ["vendor_guard.no_manifest"]


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ("not a manifest line\n", "not `<sha256>  <path>`"),
        (f"{'a' * 64}  ../x\n", "leaves the manifest's directory"),
        (f"{'a' * 64}  /etc/passwd\n", "leaves the manifest's directory"),
        (f"{'a' * 64}  x\n{'b' * 64}  x\n", "listed twice"),
    ],
)
def test_a_bad_manifest_is_an_error(
    tmp_path: Path, runner: FakeRunner, body: str, needle: str
) -> None:
    write(tmp_path, "vendor/MANIFEST.sha256", body)
    result = _run(tmp_path, runner)
    assert result.status == "error" and needle in result.issues[0].msg


def test_bad_arguments_are_an_error(tmp_path: Path, runner: FakeRunner) -> None:
    assert _run(tmp_path, runner, manifest=3).status == "error"
    assert _run(tmp_path, runner, allow="patches").status == "error"


def test_comments_and_blank_lines_are_ignored_and_dot_slash_is_accepted(tmp_path: Path) -> None:
    path = write(tmp_path, "m.sha256", f"# c\n\n{'a' * 64}  ./x/y.sv\n{'b' * 64} *z.sv\n")
    assert read_manifest(path) == {"x/y.sv": "a" * 64, "z.sv": "b" * 64}
    write(tmp_path, "bad.sha256", "zz  x\n")
    with pytest.raises(ManifestError):
        read_manifest(tmp_path / "bad.sha256")


def test_the_idempotency_key_follows_the_files(tmp_path: Path, runner: FakeRunner) -> None:
    _vendor(tmp_path, {"ip/a.sv": "x\n"})
    key = _run(tmp_path, runner).idempotency_key
    assert _run(tmp_path, runner).idempotency_key == key
    (tmp_path / "vendor/ip/a.sv").write_text("y\n")
    assert _run(tmp_path, runner).idempotency_key != key


def test_the_check_is_registered() -> None:
    from importlib.metadata import entry_points

    [ep] = [e for e in entry_points(group="chipgraph.adapters.check") if e.name == "vendor_guard"]
    assert ep.load() is VendorGuardCheck
    assert vendor_guard.DEFAULT_MANIFEST == "vendor/MANIFEST.sha256"


# --- the sample IP of tinysoc ------------------------------------------------------------------


def test_tinysoc_vendor_files_match_their_manifest(tmp_path: Path, runner: FakeRunner) -> None:
    root = tmp_path / "t"
    shutil.copytree(TINYSOC, root)
    assert _run(root, runner).status == "pass"
    # the same manifest verifies with the standard tool, in the manifest's directory
    if shutil.which("shasum"):
        done = subprocess.run(
            ["shasum", "-a", "256", "-c", "MANIFEST.sha256"],
            cwd=root / "vendor",
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stderr + done.stdout


def test_editing_the_sample_ip_fails_the_guard(tmp_path: Path, runner: FakeRunner) -> None:
    root = tmp_path / "t"
    shutil.copytree(TINYSOC, root)
    ip = root / "vendor/vendor_fifo/vendor_fifo.sv"
    ip.write_text(ip.read_text().replace("err_o", "error_o"))
    result = _run(root, runner)
    assert result.status == "fail"
    assert [(i.rule, i.file) for i in result.issues] == [
        ("vendor_guard.modified", "vendor/vendor_fifo/vendor_fifo.sv")
    ]


def test_write_manifest_reproduces_the_committed_one() -> None:
    text = write_manifest(TINYSOC / "vendor")
    committed = (TINYSOC / "vendor/MANIFEST.sha256").read_text()
    body = [line for line in committed.splitlines() if line and not line.startswith("#")]
    assert text.splitlines() == body
