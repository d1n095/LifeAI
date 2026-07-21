"""Secure ZIP import engine for Founder Knowledge Studio (see
docs/KNOWLEDGE_IMPORT_SECURITY.md for the full threat model this implements). Nothing here
writes to the database or touches app/rag/extract.py — this module's only job is to turn an
untrusted ZIP byte blob into a list of individually-vetted (filename, content, checksum)
results, or abort the whole import for a small set of violations severe enough that
continuing would be unsafe regardless of what else is in the package.

Two different failure modes, deliberately not conflated:
  - ZipSecurityError: aborts the ENTIRE import (path traversal, a zip bomb, too many
    files/too much total data). These are package-level attacks, not "this one file is
    bad" — the difference between one bad file and a package rigged to be dangerous to
    process at all.
  - A per-entry ZipEntryResult with status="skipped"/"rejected": that ONE file is excluded,
    the rest of the import proceeds normally (unsupported extension, executable, magic-byte
    mismatch, oversized single file, unreadable). This is what DEL 2's "fortsätt med säkra
    filer när policy tillåter" asks for.
"""

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

MAX_FILES = 500
MAX_TOTAL_UNCOMPRESSED_BYTES = 200 * 1024 * 1024  # 200 MB per package
MAX_SINGLE_FILE_UNCOMPRESSED_BYTES = 25 * 1024 * 1024  # matches documents.py's MAX_UPLOAD_BYTES
MAX_COMPRESSION_RATIO = 100  # uncompressed / compressed, red flag above this
MIN_COMPRESSED_SIZE_FOR_RATIO_CHECK = 256  # tiny files can legitimately compress hugely; not worth flagging
CHUNK_SIZE = 64 * 1024

# Never imported, regardless of what's inside them — Founder Knowledge Studio only ever
# needs to read text out of a package, never execute anything from it.
EXECUTABLE_EXTENSIONS = {
    ".exe", ".dll", ".so", ".bin", ".bat", ".cmd", ".com", ".msi", ".ps1", ".sh",
    ".app", ".scr", ".jar", ".apk", ".vbs", ".psm1", ".dylib",
}

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".markdown", ".json", ".html", ".htm"}

# Only checked for formats with an actual fixed binary signature — plain-text formats
# (.txt/.md/.json/.html) have no such signature, so "never trust the extension alone" for
# those means something different (they're just decoded as UTF-8 downstream, which fails
# safely on genuinely binary content) rather than a magic-byte check that doesn't exist.
MAGIC_BYTES: dict[str, list[bytes]] = {
    ".pdf": [b"%PDF-"],
    ".docx": [b"PK\x03\x04"],  # docx is itself a zip container
}


class ZipSecurityError(Exception):
    """A violation severe enough to abort the whole import — see module docstring."""


@dataclass
class ZipEntryResult:
    filename: str
    status: str  # "ok" | "skipped" | "rejected"
    reason: str | None = None
    content: bytes | None = None
    checksum: str | None = None
    uncompressed_size: int | None = None


@dataclass
class ZipImportResult:
    entries: list[ZipEntryResult] = field(default_factory=list)
    manifest: dict | None = None
    manifest_error: str | None = None

    @property
    def ok_entries(self) -> list[ZipEntryResult]:
        return [e for e in self.entries if e.status == "ok"]


def _is_safe_member_name(name: str) -> bool:
    """Zip Slip / path traversal defense. Rejects anything that could resolve outside the
    extraction root — absolute paths (POSIX or Windows drive-letter/UNC), and any path
    segment of `..`. Deliberately checks path *segments* via PurePosixPath, not a naive
    substring search for ".." (which would also false-positive on a legitimate filename like
    "my..file.pdf")."""
    if not name or name.strip() == "":
        return False
    if name.startswith("/") or name.startswith("\\"):
        return False
    if len(name) >= 2 and name[1] == ":":  # C:\... or C:/...
        return False
    if name.startswith("\\\\"):  # \\server\share UNC path
        return False
    parts = PurePosixPath(name.replace("\\", "/")).parts
    return ".." not in parts


def _magic_bytes_ok(suffix: str, content: bytes) -> bool:
    signatures = MAGIC_BYTES.get(suffix)
    if not signatures:
        return True  # no known fixed signature for this type — nothing to check
    return any(content.startswith(sig) for sig in signatures)


def _read_with_hard_cap(zf: zipfile.ZipFile, info: zipfile.ZipInfo, max_bytes: int) -> bytes:
    """Streams the entry out in fixed-size chunks and aborts the instant more than
    `max_bytes` has been produced — deliberately NOT `zf.read(info)`, which decompresses the
    entire entry into memory in one call regardless of what the central directory claims
    about its size. A crafted entry whose real (decompressed) size disagrees with its
    declared `file_size` would sail straight through a metadata-only check; this doesn't
    trust the metadata for the actual byte budget, only as a fast pre-filter (see
    validate_and_extract_zip's ratio check, which runs first and is what actually avoids
    decompressing an obvious bomb at all)."""
    produced = 0
    chunks: list[bytes] = []
    with zf.open(info) as fh:
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            produced += len(chunk)
            if produced > max_bytes:
                raise ZipSecurityError(
                    f"{info.filename!r} packar upp till mer än {max_bytes} bytes — avbryter läsningen "
                    "(möjlig zip bomb eller förfalskad storleksmetadata)."
                )
            chunks.append(chunk)
    return b"".join(chunks)


def validate_and_extract_zip(
    raw: bytes,
    *,
    max_files: int = MAX_FILES,
    max_total_bytes: int = MAX_TOTAL_UNCOMPRESSED_BYTES,
    max_file_bytes: int = MAX_SINGLE_FILE_UNCOMPRESSED_BYTES,
) -> ZipImportResult:
    """Validates and extracts every safe, supported file in `raw` (a ZIP archive's raw
    bytes). Raises ZipSecurityError for a package-level violation (see module docstring);
    otherwise returns a ZipImportResult whose `entries` list has one row per file found,
    each individually marked ok/skipped/rejected."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ZipSecurityError(f"Filen är inte ett giltigt ZIP-arkiv: {exc}") from exc

    infolist = zf.infolist()
    real_entries = [i for i in infolist if not i.is_dir()]
    if len(real_entries) > max_files:
        raise ZipSecurityError(f"För många filer i paketet ({len(real_entries)} st, max {max_files}).")

    result = ZipImportResult()
    total_uncompressed = 0

    for info in real_entries:
        name = info.filename

        if not _is_safe_member_name(name):
            raise ZipSecurityError(f"Osäker sökväg i arkivet — avbryter hela importen: {name!r}")

        if info.file_size > max_file_bytes:
            result.entries.append(
                ZipEntryResult(
                    filename=name,
                    status="rejected",
                    reason=f"Filen är för stor enligt arkivets metadata ({info.file_size} bytes, max {max_file_bytes}).",
                )
            )
            continue

        total_uncompressed += info.file_size
        if total_uncompressed > max_total_bytes:
            raise ZipSecurityError(
                f"Paketets totala uppackade storlek överskrider gränsen ({max_total_bytes} bytes) — avbryter hela importen."
            )

        # Zip-bomb pre-filter: a declared compression ratio this extreme is inspected BEFORE
        # any decompression happens at all — see _read_with_hard_cap's docstring for the
        # second, streaming layer of defense that doesn't rely on this metadata being honest.
        compressed = info.compress_size or 1
        if compressed >= MIN_COMPRESSED_SIZE_FOR_RATIO_CHECK and (info.file_size / compressed) > MAX_COMPRESSION_RATIO:
            raise ZipSecurityError(
                f"Orimlig komprimeringsgrad för {name!r} ({info.file_size / compressed:.0f}x) "
                "— avbryter hela importen (möjlig zip bomb)."
            )

        suffix = PurePosixPath(name).suffix.lower()
        if suffix in EXECUTABLE_EXTENSIONS:
            result.entries.append(ZipEntryResult(filename=name, status="skipped", reason="Körbara filer importeras aldrig."))
            continue
        if suffix not in ALLOWED_EXTENSIONS:
            result.entries.append(
                ZipEntryResult(filename=name, status="skipped", reason=f"Filtypen {suffix or '(ingen)'} stöds inte.")
            )
            continue

        try:
            content = _read_with_hard_cap(zf, info, max_file_bytes)
        except ZipSecurityError:
            raise
        except Exception as exc:  # noqa: BLE001 - a corrupt single entry must not abort the whole package
            result.entries.append(ZipEntryResult(filename=name, status="rejected", reason=f"Kunde inte läsa filen: {exc}"))
            continue

        if not _magic_bytes_ok(suffix, content):
            result.entries.append(
                ZipEntryResult(filename=name, status="rejected", reason="Filens innehåll matchar inte filändelsen.")
            )
            continue

        result.entries.append(
            ZipEntryResult(
                filename=name,
                status="ok",
                content=content,
                checksum=hashlib.sha256(content).hexdigest(),
                uncompressed_size=len(content),
            )
        )

    manifest_entry = next(
        (e for e in result.entries if PurePosixPath(e.filename).name.lower() == "manifest.json" and e.status == "ok"),
        None,
    )
    if manifest_entry is not None:
        try:
            parsed = json.loads(manifest_entry.content.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("manifest.json måste vara ett JSON-objekt.")
            result.manifest = parsed
        except Exception as exc:  # noqa: BLE001 - a broken manifest is metadata, not a security boundary
            result.manifest_error = f"manifest.json kunde inte tolkas: {exc}"

    return result


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
