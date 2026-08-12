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
  - A per-entry ZipEntryResult with status="skipped"/"rejected"/"encrypted": that ONE file
    is excluded, the rest of the import proceeds normally (unsupported extension,
    executable, magic-byte mismatch, oversized single file, unreadable, password-protected).
    This is what DEL 2's "fortsätt med säkra filer när policy tillåter" asks for.

P2 (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §10): nested ZIP archives (a zip inside a zip,
up to MAX_NESTING_DEPTH levels) are now extracted recursively instead of being silently
skipped as an unsupported file type. Every safety control below (path traversal, the two-layer
zip-bomb defense, magic bytes, executable blocking, the file-count and total-byte budgets)
applies IDENTICALLY at every nesting level — in particular, the total-byte and file-count
budgets are SHARED across all levels via the mutable _ExtractionBudget object threaded through
every recursive call, never reset per level. A nested archive whose own decompressed size
exceeds MAX_SINGLE_FILE_UNCOMPRESSED_BYTES is rejected exactly like any other oversized single
file — a nested zip IS one entry from its parent's point of view.
"""

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

# P2 kapacitetstest (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §10.7,
# tests/backend/rag/test_zip_import_capacity.py): 2500 filer i 2 nästlingsnivåer genom HELA den
# riktiga pipelinen (run_import_job, riktig Postgres, riktig storage, mockad men strukturellt
# realistisk leverantör) tog 447.7s (179.1 ms/fil), toppminne 175 MB RSS. Kostnaden är
# DB-rundresorna per fil (ett db.commit() per fil, worker_concurrency=1), inte
# uppackningen — som väntat. 500 filer skalar därför till runt 90s i detta bästa-scenario.
# MEDVETET INTE HÖJD: den här mätningen mockar embedding/chat-anropen, så den mäter INTE den
# riktiga leverantörens nätverkslatens per fil (som i produktion sannolikt dominerar över
# DB-kostnaden) — att höja gränsen på den här siffran ensam vore precis den gissning §10.7
# uttryckligen förbjuder. 500 kvarstår som en medvetet konservativ, uppmätt-säker gräns tills
# ett motsvarande test körs mot en riktig leverantör.
MAX_FILES = 500
MAX_TOTAL_UNCOMPRESSED_BYTES = 200 * 1024 * 1024  # 200 MB per package, SHARED across all nesting levels
MAX_SINGLE_FILE_UNCOMPRESSED_BYTES = 25 * 1024 * 1024  # matches documents.py's MAX_UPLOAD_BYTES, applies at every level
MAX_COMPRESSION_RATIO = 100  # uncompressed / compressed, red flag above this
MIN_COMPRESSED_SIZE_FOR_RATIO_CHECK = 256  # tiny files can legitimately compress hugely; not worth flagging
CHUNK_SIZE = 64 * 1024
# P2: how many levels of "zip inside zip" are followed before a nested archive is rejected
# outright — depth 0 is the top-level package itself, so this allows nesting depth 1..3.
# Deliberately small: legitimate exports rarely nest archives more than once or twice: this
# exists to bound worst-case recursion/processing cost, not to support arbitrary structures.
MAX_NESTING_DEPTH = 3
# P2: the separator between archive boundaries in a human-readable archive_path
# (e.g. "backup.zip!/users/docs.zip!/contracts/lease.pdf") — deliberately distinct from the
# ordinary "/" a path uses within one archive, so a founder (or a future citation UI) can
# never mistake "a subfolder" for "a different, nested archive". Same convention JAR URLs
# already use for nested archives — not a new invention.
ARCHIVE_PATH_SEPARATOR = "!/"

# Never imported, regardless of what's inside them — Founder Knowledge Studio only ever
# needs to read text out of a package, never execute anything from it.
EXECUTABLE_EXTENSIONS = {
    ".exe", ".dll", ".so", ".bin", ".bat", ".cmd", ".com", ".msi", ".ps1", ".sh",
    ".app", ".scr", ".jar", ".apk", ".vbs", ".psm1", ".dylib",
}

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".markdown", ".json", ".html", ".htm", ".csv"}

# docs/LIFE_SOURCE_FOUNDATION_BOOTSTRAP.md §F: CSV is a plain-text, deterministically parseable
# format needing no new dependency (app/rag/extract.py's existing UTF-8-decode fallback already
# handles it correctly, the same as .txt/.md/.json) -- so it's added here as a pure config
# change, not new extraction code. XLSX is deliberately NOT added: it's a binary format that
# would require a new third-party dependency (e.g. openpyxl), which is a supply-chain decision
# this bootstrap pass does not make unilaterally -- see that doc's §F for the explicit
# "where an extractor exists or can be safely introduced" framing this follows.

# Only checked for formats with an actual fixed binary signature — plain-text formats
# (.txt/.md/.json/.html/.csv) have no such signature, so "never trust the extension alone" for
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
    status: str  # "ok" | "skipped" | "rejected" | "encrypted"
    reason: str | None = None
    content: bytes | None = None
    checksum: str | None = None
    uncompressed_size: int | None = None
    # P2: only set (non-None) for a file that came from INSIDE a nested archive — a
    # top-level file's archive_path/archive_chain are both None, unchanged from before P2.
    # See docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §10.6 for the exact format/determinism
    # contract these two fields must uphold.
    archive_path: str | None = None
    archive_chain: list[dict] | None = None


@dataclass
class ZipImportResult:
    entries: list[ZipEntryResult] = field(default_factory=list)
    manifest: dict | None = None
    manifest_error: str | None = None

    @property
    def ok_entries(self) -> list[ZipEntryResult]:
        return [e for e in self.entries if e.status == "ok"]


class _ExtractionBudget:
    """P2: the shared, mutable accumulator threaded through every recursive extraction call —
    the concrete mechanism behind "delad, inte per-nivå" (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md
    §10.5). A single instance is created once, in validate_and_extract_zip(), and every nested
    call mutates the SAME instance — a nested level's byte/file counts are ADDED to whatever
    the outer levels already used, never reset to zero. This is what makes a "small per level,
    large in total" nested zip bomb impossible: the budget has no concept of "level" at all,
    only a running total."""

    __slots__ = ("total_uncompressed_bytes", "total_files")

    def __init__(self) -> None:
        self.total_uncompressed_bytes = 0
        self.total_files = 0


def _is_safe_member_name(name: str) -> bool:
    """Zip Slip / path traversal defense. Rejects anything that could resolve outside the
    extraction root — absolute paths (POSIX or Windows drive-letter/UNC), and any path
    segment of `..`. Deliberately checks path *segments* via PurePosixPath, not a naive
    substring search for ".." (which would also false-positive on a legitimate filename like
    "my..file.pdf"). Applied identically to every entry at every nesting level — a nested
    archive's own internal names get exactly the same scrutiny as the top-level package's."""
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


def _sanitize_outer_filename(name: str) -> str:
    """The top-level package's own filename (`outer_filename`, e.g. ImportJob.source_filename)
    comes straight from an untrusted HTTP upload (app/routers/library.py's `file.filename`) —
    unlike every in-archive member name, it is NEVER run through _is_safe_member_name(). It
    only ever becomes the first, label-only segment of a nested file's archive_path (§10.6),
    never a real filesystem path, but a crafted upload name could otherwise inject a literal
    ".."/absolute-path prefix or even a fake ARCHIVE_PATH_SEPARATOR ("!/") into that
    provenance string. Taking only the final path component (after normalizing backslashes)
    strips any directory prefix — including any "..", any absolute-path prefix, and any "/"
    that could form a forged "!/" boundary — leaving just a plain display name, exactly like
    every in-archive segment already gets via _normalize_member_name()."""
    normalized = name.replace("\\", "/")
    base = PurePosixPath(normalized).name
    return base or "archive.zip"


def _normalize_member_name(name: str) -> str:
    """P2: the single normalization step archive_path segments are built from — always
    forward slashes, regardless of how the archive itself stored the entry (some
    Windows-authored zip tools use backslashes). Only ever called on a name that has already
    passed _is_safe_member_name(), so the result is guaranteed free of `..`, absolute paths,
    and drive letters — normalization here is about SEPARATOR STYLE only, not a second
    security check."""
    return name.replace("\\", "/")


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
    decompressing an obvious bomb at all). Also where a password-protected entry's
    RuntimeError/NotImplementedError first surfaces — see _extract_recursive's classification
    of that exception into status="encrypted"."""
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


def _classify_unreadable_entry(exc: Exception) -> tuple[str, str]:
    """P2: distinguishes a password-protected entry from every other reason zipfile might
    fail to read one — never guesses "encrypted" for an unrelated failure. Python's zipfile
    raises RuntimeError (ZipCrypto) or NotImplementedError (an unsupported, often AES,
    encryption/compression method) when a password is required; both exception TYPES can
    also be raised for unrelated reasons, so the message itself is what's actually checked."""
    text = str(exc).lower()
    if "encrypt" in text or "password" in text:
        return "encrypted", "Filen/arkivet är lösenordsskyddat — kan inte läsas automatiskt."
    return "rejected", f"Kunde inte läsa filen: {exc}"


def _extract_recursive(
    zf: zipfile.ZipFile,
    *,
    depth: int,
    chain_prefix: list[dict],
    budget: _ExtractionBudget,
    max_files: int,
    max_total_bytes: int,
    max_file_bytes: int,
    max_nesting_depth: int,
    entries_out: list[ZipEntryResult],
) -> None:
    """Processes one already-open ZipFile's entries, appending a ZipEntryResult per file to
    `entries_out` (a single, flat, shared list across every nesting level — the caller never
    sees or needs to know how many levels produced it). Recurses into a `.zip` entry instead
    of skipping it as an unsupported type, subject to `max_nesting_depth` and the SHARED
    `budget` (see _ExtractionBudget's docstring). Raises ZipSecurityError for a package-level
    violation, at any depth — identical semantics to the pre-P2, non-recursive version."""
    infolist = zf.infolist()
    real_entries = [i for i in infolist if not i.is_dir()]

    for info in real_entries:
        name = info.filename

        if not _is_safe_member_name(name):
            raise ZipSecurityError(f"Osäker sökväg i arkivet — avbryter hela importen: {name!r}")

        normalized_name = _normalize_member_name(name)

        budget.total_files += 1
        if budget.total_files > max_files:
            raise ZipSecurityError(f"För många filer i paketet (minst {budget.total_files} st, max {max_files}).")

        # Per-file size cap — applies identically at every nesting level, including to a
        # nested archive's own declared size (it is one entry from its parent's perspective).
        if info.file_size > max_file_bytes:
            entries_out.append(
                ZipEntryResult(
                    filename=name,
                    status="rejected",
                    reason=f"Filen är för stor enligt arkivets metadata ({info.file_size} bytes, max {max_file_bytes}).",
                )
            )
            continue

        # Shared, cross-level total-byte budget — see _ExtractionBudget's docstring for why
        # this must never be reset per nesting level.
        budget.total_uncompressed_bytes += info.file_size
        if budget.total_uncompressed_bytes > max_total_bytes:
            raise ZipSecurityError(
                f"Paketets totala uppackade storlek överskrider gränsen ({max_total_bytes} bytes) — avbryter hela importen."
            )

        # Zip-bomb pre-filter: a declared compression ratio this extreme is inspected BEFORE
        # any decompression happens at all — see _read_with_hard_cap's docstring for the
        # second, streaming layer of defense that doesn't rely on this metadata being honest.
        # Applies at every level, unchanged.
        compressed = info.compress_size or 1
        if compressed >= MIN_COMPRESSED_SIZE_FOR_RATIO_CHECK and (info.file_size / compressed) > MAX_COMPRESSION_RATIO:
            raise ZipSecurityError(
                f"Orimlig komprimeringsgrad för {name!r} ({info.file_size / compressed:.0f}x) "
                "— avbryter hela importen (möjlig zip bomb)."
            )

        suffix = PurePosixPath(name).suffix.lower()
        if suffix in EXECUTABLE_EXTENSIONS:
            entries_out.append(ZipEntryResult(filename=name, status="skipped", reason="Körbara filer importeras aldrig."))
            continue

        is_nested_zip = suffix == ".zip"
        if not is_nested_zip and suffix not in ALLOWED_EXTENSIONS:
            entries_out.append(
                ZipEntryResult(filename=name, status="skipped", reason=f"Filtypen {suffix or '(ingen)'} stöds inte.")
            )
            continue

        # From here the entry is either a nested archive or a supported leaf type — its bytes
        # must actually be read (the earlier checks were metadata-only).
        try:
            content = _read_with_hard_cap(zf, info, max_file_bytes)
        except ZipSecurityError:
            raise
        except (RuntimeError, NotImplementedError) as exc:
            status, reason = _classify_unreadable_entry(exc)
            entries_out.append(ZipEntryResult(filename=name, status=status, reason=reason))
            continue
        except Exception as exc:  # noqa: BLE001 - a corrupt single entry must not abort the whole package
            entries_out.append(ZipEntryResult(filename=name, status="rejected", reason=f"Kunde inte läsa filen: {exc}"))
            continue

        if is_nested_zip:
            if depth >= max_nesting_depth:
                entries_out.append(
                    ZipEntryResult(
                        filename=name,
                        status="rejected",
                        reason=f"Nästlingsdjupet överskrider gränsen (max {max_nesting_depth} nivåer).",
                    )
                )
                continue
            try:
                nested_zf = zipfile.ZipFile(io.BytesIO(content))
            except zipfile.BadZipFile:
                entries_out.append(
                    ZipEntryResult(
                        filename=name, status="rejected", reason="Nästlat arkiv kunde inte läsas (skadat eller inte en riktig ZIP)."
                    )
                )
                continue
            _extract_recursive(
                nested_zf,
                depth=depth + 1,
                chain_prefix=chain_prefix + [{"filename": normalized_name, "checksum": hashlib.sha256(content).hexdigest()}],
                budget=budget,
                max_files=max_files,
                max_total_bytes=max_total_bytes,
                max_file_bytes=max_file_bytes,
                max_nesting_depth=max_nesting_depth,
                entries_out=entries_out,
            )
            continue

        if not _magic_bytes_ok(suffix, content):
            entries_out.append(
                ZipEntryResult(filename=name, status="rejected", reason="Filens innehåll matchar inte filändelsen.")
            )
            continue

        checksum = hashlib.sha256(content).hexdigest()
        if depth == 0:
            # Top-level file, no enclosing nested archive — archive_path/archive_chain stay
            # None, byte-for-byte the same behavior as before P2.
            archive_path = None
            archive_chain = None
        else:
            full_chain = chain_prefix + [{"filename": normalized_name, "checksum": checksum}]
            archive_path = ARCHIVE_PATH_SEPARATOR.join(seg["filename"] for seg in full_chain)
            archive_chain = full_chain

        entries_out.append(
            ZipEntryResult(
                filename=name,
                status="ok",
                content=content,
                checksum=checksum,
                uncompressed_size=len(content),
                archive_path=archive_path,
                archive_chain=archive_chain,
            )
        )


def validate_and_extract_zip(
    raw: bytes,
    *,
    outer_filename: str = "archive.zip",
    max_files: int = MAX_FILES,
    max_total_bytes: int = MAX_TOTAL_UNCOMPRESSED_BYTES,
    max_file_bytes: int = MAX_SINGLE_FILE_UNCOMPRESSED_BYTES,
    max_nesting_depth: int = MAX_NESTING_DEPTH,
) -> ZipImportResult:
    """Validates and extracts every safe, supported file in `raw` (a ZIP archive's raw
    bytes), recursing into nested archives up to `max_nesting_depth` levels (P2). Raises
    ZipSecurityError for a package-level violation (see module docstring); otherwise returns
    a ZipImportResult whose `entries` list has one row per file found across every nesting
    level (a single flat list — see _extract_recursive's docstring), each individually marked
    ok/skipped/rejected/encrypted.

    `outer_filename` is the top-level package's own name (e.g. `ImportJob.source_filename`,
    itself an untrusted HTTP upload filename — see _sanitize_outer_filename()) — used ONLY as
    the first segment of a nested file's archive_path (see
    docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §10.6), always sanitized first; it never affects
    validation, and top-level (non-nested) entries never reference it at all."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ZipSecurityError(f"Filen är inte ett giltigt ZIP-arkiv: {exc}") from exc

    result = ZipImportResult()
    budget = _ExtractionBudget()
    _extract_recursive(
        zf,
        depth=0,
        chain_prefix=[{"filename": _sanitize_outer_filename(outer_filename), "checksum": sha256_bytes(raw)}],
        budget=budget,
        max_files=max_files,
        max_total_bytes=max_total_bytes,
        max_file_bytes=max_file_bytes,
        max_nesting_depth=max_nesting_depth,
        entries_out=result.entries,
    )

    # manifest.json describes the TOP-LEVEL package only — restricted to depth-0 entries
    # (archive_path is None only there) so a nested archive can never smuggle in a rogue
    # manifest.json that gets treated as authoritative for the whole import.
    manifest_entry = next(
        (
            e
            for e in result.entries
            if e.archive_path is None and PurePosixPath(e.filename).name.lower() == "manifest.json" and e.status == "ok"
        ),
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
