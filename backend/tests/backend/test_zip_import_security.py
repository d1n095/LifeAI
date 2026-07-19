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
