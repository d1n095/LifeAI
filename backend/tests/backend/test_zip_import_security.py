"""Security tests for app/rag/zip_import.py — see docs/KNOWLEDGE_IMPORT_SECURITY.md. Every
test here builds a real ZIP in memory (zipfile.ZipFile) and feeds real bytes through the real
validator; nothing is mocked. Each attack class gets its own test so a regression in one
defense doesn't get masked by another."""

import io
import zipfile

import pytest

from app.rag.zip_import import (
    MAX_COMPRESSION_RATIO,
    ZipSecurityError,
    sha256_bytes,
    validate_and_extract_zip,
)


def _make_zip(files: dict[str, bytes], *, compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_accepts_a_normal_small_package():
    raw = _make_zip({"a.txt": b"hej", "b.md": b"# rubrik", "sub/c.txt": b"nastlad fil"})
    result = validate_and_extract_zip(raw)
    assert len(result.ok_entries) == 3
    names = {e.filename for e in result.ok_entries}
    assert names == {"a.txt", "b.md", "sub/c.txt"}


def test_rejects_path_traversal_dotdot():
    raw = _make_zip({"../../etc/passwd": b"pwned"})
    with pytest.raises(ZipSecurityError, match="Osäker sökväg"):
        validate_and_extract_zip(raw)


def test_rejects_path_traversal_nested_dotdot():
    raw = _make_zip({"innocent/../../escape.txt": b"pwned"})
    with pytest.raises(ZipSecurityError, match="Osäker sökväg"):
        validate_and_extract_zip(raw)


def test_rejects_absolute_unix_path():
    raw = _make_zip({"/etc/passwd": b"pwned"})
    with pytest.raises(ZipSecurityError, match="Osäker sökväg"):
        validate_and_extract_zip(raw)


def test_rejects_windows_drive_letter_path():
    raw = _make_zip({"C:/Windows/System32/evil.txt": b"pwned"})
    with pytest.raises(ZipSecurityError, match="Osäker sökväg"):
        validate_and_extract_zip(raw)


def test_does_not_false_positive_on_literal_dots_in_filename():
    """".." as a substring (not a path segment) inside an otherwise normal filename must not
    be rejected — the check is segment-aware, not a naive substring search."""
    raw = _make_zip({"my..file.txt": b"legitimate content"})
    result = validate_and_extract_zip(raw)
    assert len(result.ok_entries) == 1
    assert result.ok_entries[0].filename == "my..file.txt"


def test_rejects_too_many_files():
    files = {f"file{i}.txt": b"x" for i in range(10)}
    raw = _make_zip(files)
    with pytest.raises(ZipSecurityError, match="För många filer"):
        validate_and_extract_zip(raw, max_files=5)


def test_rejects_total_uncompressed_size_over_limit():
    raw = _make_zip({"a.txt": b"x" * 1000, "b.txt": b"x" * 1000})
    with pytest.raises(ZipSecurityError, match="totala uppackade storlek"):
        validate_and_extract_zip(raw, max_total_bytes=1500)


def test_single_oversized_file_is_rejected_not_fatal_to_the_whole_import():
    """A too-big single file is a per-entry rejection (status="rejected"), NOT an abort —
    unlike the security-severity violations above, this doesn't indicate malice, just a file
    that doesn't fit the policy, so the rest of the package still imports."""
    raw = _make_zip({"huge.txt": b"x" * 5000, "normal.txt": b"fine"})
    result = validate_and_extract_zip(raw, max_file_bytes=1000)
    rejected = [e for e in result.entries if e.status == "rejected"]
    ok = [e for e in result.entries if e.status == "ok"]
    assert [e.filename for e in rejected] == ["huge.txt"]
    assert [e.filename for e in ok] == ["normal.txt"]


def test_rejects_extreme_compression_ratio_as_possible_zip_bomb():
    """A real zip bomb: a large run of a single repeated byte compresses to a tiny footprint
    under DEFLATE (a genuine, not contrived, several-thousand-times ratio) — this must be
    caught by the ratio pre-filter before any full decompression happens."""
    bomb_content = b"\x00" * (50 * 1024 * 1024)  # 50 MB of zeros compresses to a few KB
    raw = _make_zip({"bomb.txt": bomb_content})
    with pytest.raises(ZipSecurityError, match="[Kk]omprimeringsgrad|zip bomb"):
        validate_and_extract_zip(raw, max_file_bytes=100 * 1024 * 1024)


def test_hard_cap_streaming_read_catches_a_bomb_even_if_ratio_check_were_bypassed():
    """Defense in depth: directly exercises _read_with_hard_cap's byte-budget abort, the
    second, independent layer behind the metadata-based ratio pre-filter (see
    zip_import.py's docstring on why decompression itself is capped, not just inspected via
    declared sizes beforehand)."""
    from app.rag.zip_import import _read_with_hard_cap

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.txt", b"a" * (2 * 1024 * 1024))
    zf = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    info = zf.infolist()[0]

    with pytest.raises(ZipSecurityError, match="packar upp till mer"):
        _read_with_hard_cap(zf, info, max_bytes=1024)


def test_ignores_executable_files_regardless_of_content():
    raw = _make_zip({"innocent.txt": b"hej", "malware.exe": b"MZ\x90\x00fake-pe-header"})
    result = validate_and_extract_zip(raw)
    skipped = {e.filename: e for e in result.entries if e.status == "skipped"}
    assert "malware.exe" in skipped
    assert "körbara" in skipped["malware.exe"].reason.lower()
    assert [e.filename for e in result.ok_entries] == ["innocent.txt"]


def test_skips_unsupported_file_types():
    raw = _make_zip({"data.csv": b"a,b,c", "readme.txt": b"ok"})
    result = validate_and_extract_zip(raw)
    assert [e.filename for e in result.ok_entries] == ["readme.txt"]
    assert any(e.filename == "data.csv" and e.status == "skipped" for e in result.entries)


def test_never_trusts_extension_alone_rejects_content_mismatched_pdf():
    """A file named *.pdf whose content is NOT actually a PDF (no %PDF- header) must be
    rejected, not extracted and treated as if extract_text() would happily parse it —
    exactly the "aldrig lita på filändelsen ensam" requirement."""
    raw = _make_zip({"fake.pdf": b"this is definitely not a real pdf file"})
    result = validate_and_extract_zip(raw)
    assert result.entries[0].status == "rejected"
    assert "matchar inte" in result.entries[0].reason


def test_accepts_a_real_looking_pdf_header():
    raw = _make_zip({"real.pdf": b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\nfake but header-valid pdf bytes"})
    result = validate_and_extract_zip(raw)
    assert result.ok_entries[0].filename == "real.pdf"


def test_plain_text_types_have_no_magic_byte_requirement():
    """.txt/.md/.json/.html have no fixed binary signature — "never trust the extension
    alone" for these means safe UTF-8 decoding downstream, not a magic-byte check that
    doesn't exist for arbitrary text. Confirms they aren't spuriously rejected."""
    raw = _make_zip({"a.txt": "hej då".encode("utf-8"), "b.md": b"# Rubrik", "c.json": b'{"a": 1}', "d.html": b"<p>hej</p>"})
    result = validate_and_extract_zip(raw)
    assert len(result.ok_entries) == 4


def test_checksum_is_deterministic_and_content_addressed():
    raw = _make_zip({"a.txt": b"samma innehall"})
    result1 = validate_and_extract_zip(raw)
    result2 = validate_and_extract_zip(raw)
    assert result1.ok_entries[0].checksum == result2.ok_entries[0].checksum
    assert result1.ok_entries[0].checksum == sha256_bytes(b"samma innehall")


def test_bad_zip_bytes_raise_cleanly():
    with pytest.raises(ZipSecurityError, match="inte ett giltigt ZIP"):
        validate_and_extract_zip(b"this is not a zip file at all")


def test_manifest_json_is_parsed_when_present_and_valid():
    manifest = b'{"package": "test-fkp", "documents": [{"file": "a.txt", "category": "general"}]}'
    raw = _make_zip({"manifest.json": manifest, "a.txt": b"hej"})
    result = validate_and_extract_zip(raw)
    assert result.manifest == {"package": "test-fkp", "documents": [{"file": "a.txt", "category": "general"}]}
    assert result.manifest_error is None


def test_manifest_json_parse_failure_is_non_fatal():
    """A broken manifest.json is metadata, not a security boundary — the rest of the
    package must still import, with the parse failure surfaced for the caller to decide
    what to do with (see app/rag/library_import.py)."""
    raw = _make_zip({"manifest.json": b"{not valid json", "a.txt": b"hej"})
    result = validate_and_extract_zip(raw)
    assert result.manifest is None
    assert result.manifest_error is not None
    assert any(e.filename == "a.txt" and e.status == "ok" for e in result.entries)


def test_manifest_json_that_is_not_an_object_is_rejected_as_metadata_error():
    raw = _make_zip({"manifest.json": b"[1, 2, 3]"})
    result = validate_and_extract_zip(raw)
    assert result.manifest is None
    assert result.manifest_error is not None


def test_default_compression_ratio_constant_is_reasonable():
    # Sanity check on the module's own default — not a magic number nobody looked at.
    assert 10 <= MAX_COMPRESSION_RATIO <= 1000


# --- P2: nested ZIP handling, encryption detection, archive_path/archive_chain provenance ---
# See docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §10 for the full spec these tests verify.


def _make_encrypted_zip(files: dict[str, bytes], password: bytes) -> bytes:
    """pyzipfile can't write real AES/ZipCrypto-encrypted entries, so this shells out to the
    system `zip` binary to produce a genuinely password-protected archive — anything less
    would test our own mock, not the real RuntimeError/NotImplementedError zipfile raises for
    an actually encrypted entry."""
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    if shutil.which("zip") is None:
        pytest.skip("system 'zip' binary not available to build a genuinely encrypted archive")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for name, content in files.items():
            file_path = tmp_path / name
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(content)
        archive_path = tmp_path / "out.zip"
        subprocess.run(
            ["zip", "-P", password.decode(), "-r", str(archive_path), "."],
            cwd=tmp_path,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return archive_path.read_bytes()


def test_nested_zip_one_level_extracts_and_indexes_normally():
    inner = _make_zip({"contract.txt": b"lease terms"})
    raw = _make_zip({"backup.zip": inner, "top.txt": b"top level"})
    result = validate_and_extract_zip(raw, outer_filename="backup.zip")
    ok_names = {e.filename for e in result.ok_entries}
    assert ok_names == {"top.txt", "contract.txt"}
    nested = next(e for e in result.ok_entries if e.filename == "contract.txt")
    assert nested.archive_path == "backup.zip!/backup.zip!/contract.txt"
    top = next(e for e in result.ok_entries if e.filename == "top.txt")
    assert top.archive_path is None
    assert top.archive_chain is None


def test_nested_zip_at_max_depth_boundary_still_extracts():
    level3 = _make_zip({"deepest.txt": b"bottom of the well"})
    level2 = _make_zip({"level3.zip": level3})
    level1 = _make_zip({"level2.zip": level2})
    raw = _make_zip({"level1.zip": level1})
    result = validate_and_extract_zip(raw, max_nesting_depth=3)
    assert any(e.filename == "deepest.txt" and e.status == "ok" for e in result.entries)


def test_nested_zip_exceeding_max_depth_is_rejected_not_the_whole_import():
    level3 = _make_zip({"deepest.txt": b"too deep"})
    level2 = _make_zip({"level3.zip": level3})
    level1 = _make_zip({"level2.zip": level2, "sibling.txt": b"still fine"})
    raw = _make_zip({"level1.zip": level1})
    result = validate_and_extract_zip(raw, max_nesting_depth=1)
    rejected = [e for e in result.entries if e.status == "rejected" and "ästlingsdjup" in (e.reason or "")]
    assert len(rejected) == 1
    assert any(e.filename == "sibling.txt" and e.status == "ok" for e in result.entries)


def test_nested_zip_shared_budget_rejects_a_bomb_that_looks_small_per_level():
    """The critical P2 security property: three SIBLING nested archives, each individually
    well under the total-byte budget on its own, must still be caught once their combined
    size crosses it — proving the budget is genuinely SHARED across every nested archive
    (not reset or evaluated independently per nesting branch). Each inner archive's own
    content (40_001 incompressible-ish bytes) alone would pass a 100_000-byte budget; three
    of them (120_003 bytes total) must not."""
    per_file_bytes = 40_001
    # os.urandom, not a repeated byte — DEFLATE would otherwise compress a repeated byte to
    # almost nothing, but info.file_size (what the budget counts) is the UNCOMPRESSED size
    # regardless, so this choice only matters for making the intent of the test obvious.
    import os

    nested_1 = _make_zip({"leaf.txt": os.urandom(per_file_bytes)})
    nested_2 = _make_zip({"leaf.txt": os.urandom(per_file_bytes)})
    nested_3 = _make_zip({"leaf.txt": os.urandom(per_file_bytes)})
    raw = _make_zip({"a.zip": nested_1, "b.zip": nested_2, "c.zip": nested_3})
    with pytest.raises(ZipSecurityError, match="totala uppackade storlek"):
        validate_and_extract_zip(raw, max_total_bytes=100_000, max_file_bytes=1_000_000)


def test_corrupt_nested_archive_is_rejected_as_one_entry_not_the_whole_batch():
    corrupt_zip_bytes = b"PK\x03\x04" + b"not actually a valid central directory"
    raw = _make_zip({"broken.zip": corrupt_zip_bytes, "fine.txt": b"still imports"})
    result = validate_and_extract_zip(raw)
    rejected = [e for e in result.entries if e.filename == "broken.zip"]
    assert len(rejected) == 1
    assert rejected[0].status == "rejected"
    assert any(e.filename == "fine.txt" and e.status == "ok" for e in result.entries)


def test_encrypted_top_level_entry_gets_a_distinct_encrypted_status_not_generic_rejected():
    raw = _make_encrypted_zip({"secret.txt": b"classified"}, password=b"hunter2")
    result = validate_and_extract_zip(raw)
    encrypted = [e for e in result.entries if e.status == "encrypted"]
    assert len(encrypted) == 1
    assert "lösenordsskyddat" in encrypted[0].reason.lower()


def test_encrypted_entry_inside_a_nested_archive_is_still_correctly_classified():
    inner = _make_encrypted_zip({"secret.txt": b"classified"}, password=b"hunter2")
    raw = _make_zip({"vault.zip": inner})
    result = validate_and_extract_zip(raw)
    encrypted = [e for e in result.entries if e.status == "encrypted"]
    assert len(encrypted) == 1
    assert encrypted[0].filename == "secret.txt"


def test_encrypted_status_is_never_treated_as_resumable_by_the_worker():
    """A minimal sanity check that "encrypted" is disjoint from "ok" — the worker/import
    orchestrator (app/rag/library_import.py) treats any non-"ok" ZipEntryResult as excluded
    from the batch, so this alone is what keeps an encrypted file out of the index."""
    raw = _make_encrypted_zip({"secret.txt": b"classified"}, password=b"hunter2")
    result = validate_and_extract_zip(raw)
    assert result.ok_entries == []


def test_archive_chain_provenance_is_recorded_correctly_for_a_nested_file():
    inner = _make_zip({"contracts/lease.pdf": b"%PDF-1.4\nfake but valid header"})
    raw = _make_zip({"users/docs.zip": inner})
    outer_checksum = sha256_bytes(raw)
    inner_checksum = sha256_bytes(inner)
    result = validate_and_extract_zip(raw, outer_filename="backup.zip")
    entry = next(e for e in result.ok_entries if e.filename == "contracts/lease.pdf")
    assert entry.archive_chain == [
        {"filename": "backup.zip", "checksum": outer_checksum},
        {"filename": "users/docs.zip", "checksum": inner_checksum},
        {"filename": "contracts/lease.pdf", "checksum": entry.checksum},
    ]


def test_archive_path_matches_the_documented_format_exactly():
    inner = _make_zip({"contracts/lease.pdf": b"%PDF-1.4\nfake but valid header"})
    raw = _make_zip({"users/docs.zip": inner})
    result = validate_and_extract_zip(raw, outer_filename="backup.zip")
    entry = next(e for e in result.ok_entries if e.filename == "contracts/lease.pdf")
    assert entry.archive_path == "backup.zip!/users/docs.zip!/contracts/lease.pdf"


def test_archive_path_is_deterministic_across_repeated_imports_of_identical_bytes():
    inner = _make_zip({"a.txt": b"same content"})
    raw = _make_zip({"nested.zip": inner})
    result1 = validate_and_extract_zip(raw, outer_filename="pkg.zip")
    result2 = validate_and_extract_zip(raw, outer_filename="pkg.zip")
    path1 = next(e for e in result1.ok_entries if e.filename == "a.txt").archive_path
    path2 = next(e for e in result2.ok_entries if e.filename == "a.txt").archive_path
    assert path1 == path2 == "pkg.zip!/nested.zip!/a.txt"


def _archive_path_segments(path: str) -> list[str]:
    # Splits on both the archive-boundary separator and ordinary "/" so ".." can be checked
    # as a genuine path segment, not just a substring.
    segments: list[str] = []
    for archive_segment in path.split("!/"):
        segments.extend(archive_segment.split("/"))
    return segments


def test_archive_path_never_contains_raw_backslashes_or_traversal_segments():
    inner = _make_zip({"sub/deep.txt": b"safe nested path"})
    raw = _make_zip({"nested.zip": inner})
    result = validate_and_extract_zip(raw, outer_filename="pkg.zip")
    entry = next(e for e in result.ok_entries if e.filename == "sub/deep.txt")
    assert "\\" not in entry.archive_path
    assert ".." not in _archive_path_segments(entry.archive_path)


def test_outer_filename_traversal_attempt_is_sanitized_out_of_archive_path():
    """outer_filename comes straight from an untrusted HTTP upload filename (see
    app/routers/library.py's file.filename) and is never run through _is_safe_member_name()
    the way an in-archive entry name is — this proves a crafted upload name can't inject a
    ".."/absolute-path prefix, nor a fake ARCHIVE_PATH_SEPARATOR ("!/") boundary, into the
    resulting archive_path."""
    inner = _make_zip({"a.txt": b"content"})
    raw = _make_zip({"nested.zip": inner})

    result = validate_and_extract_zip(raw, outer_filename="../../etc/passwd.zip")
    entry = next(e for e in result.ok_entries if e.filename == "a.txt")
    assert entry.archive_path == "passwd.zip!/nested.zip!/a.txt"
    assert ".." not in entry.archive_path

    result2 = validate_and_extract_zip(raw, outer_filename="evil!/injected.zip")
    entry2 = next(e for e in result2.ok_entries if e.filename == "a.txt")
    # The crafted name's own embedded "/" is stripped along with everything before it (only
    # the final path component survives) — it can never reconstruct a forged "!/" boundary.
    assert entry2.archive_path == "injected.zip!/nested.zip!/a.txt"


def test_top_level_files_have_no_archive_path_unchanged_from_before_p2():
    raw = _make_zip({"a.txt": b"top level, no nesting involved"})
    result = validate_and_extract_zip(raw)
    assert result.ok_entries[0].archive_path is None
    assert result.ok_entries[0].archive_chain is None


def test_existing_500_file_and_200mb_limits_still_enforced_at_top_level():
    files = {f"file{i}.txt": b"x" for i in range(10)}
    raw = _make_zip(files)
    with pytest.raises(ZipSecurityError, match="För många filer"):
        validate_and_extract_zip(raw, max_files=5)
    raw2 = _make_zip({"a.txt": b"x" * 1000, "b.txt": b"x" * 1000})
    with pytest.raises(ZipSecurityError, match="totala uppackade storlek"):
        validate_and_extract_zip(raw2, max_total_bytes=1500)
