#!/usr/bin/env python3
"""Encrypted, verifiable, provider-neutral off-host backup for the Founder Alpha database.

`db_recovery.py` already answers "is this dump restorable?" for a backup sitting on the same
disk as the database. That is not disaster recovery: the disk, the host and the copy fail
together. This tool adds the missing half — get a verified, *encrypted* artifact to a different
location, prove it survived the trip, keep a bounded history, and restore it back.

Design constraints, in order of importance
------------------------------------------
1. **The backup is encrypted before it leaves the host.** A database dump contains the
   founder's conversations, documents and embeddings. The destination is assumed hostile or at
   least insufficiently controlled (object storage, a mounted remote, an operator's disk).
   AES-256-GCM, chunked streaming, per-chunk nonces, authenticated header. An operator-supplied
   32-byte key (env `MAINAI_BACKUP_ENCRYPTION_KEY` or `--key-file`); no key material is ever
   written into the artifact or the transport manifest.
2. **Integrity is checked at every hop.** The plaintext dump's sha256 is authenticated inside
   the encrypted header; the encrypted artifact's sha256 is recorded in a separate checksum
   file *and* in the transport manifest; the transport manifest carries its own checksum. A
   truncated, swapped or bit-flipped artifact fails before decryption or restore is trusted.
3. **The dump must already be a verified restorable backup.** `push` runs
   `db_recovery.verify()` before encrypting. Uploading an unverified file would just move an
   untested claim off-host.
4. **Retention never guesses.** Pruning only deletes files named by a valid transport manifest,
   and refuses entirely if any manifest in the destination cannot be parsed. An unrelated file
   in the destination is never touched.
5. **Failures are loud.** Non-zero exit and an optional webhook (`MAINAI_BACKUP_ALERT_WEBHOOK`)
   so a failed backup is an alert, not a silent gap discovered during a restore.

Destinations
------------
    file:///absolute/path        a different disk / mounted remote / test harness
    s3://bucket/prefix           any S3-compatible endpoint via the `aws` CLI

`file://` is the controlled remote-equivalent used to exercise the whole pipeline here. It is
NOT proof of off-host retention: that claim needs a real destination that a host loss cannot
take with it, and the report says so instead of pretending otherwise.

Usage
-----
    python scripts/ops/offsite_backup.py push \
        --database-url "$DATABASE_URL" --destination file:///mnt/offsite/mainai --keep 7
    python scripts/ops/offsite_backup.py verify --destination ... --name <name>
    python scripts/ops/offsite_backup.py restore --destination ... --name <name> \
        --target-url "$RESTORE_URL" --confirm
    python scripts/ops/offsite_backup.py prune --destination ... --keep 7

Exit codes: 0 success, 1 failure, 2 refusal (unsafe request or missing key).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db_recovery  # noqa: E402  (same-toolbox import; see module docstring)

TOOL = "mainai-offsite-backup"
FORMAT_VERSION = 1
MAGIC = b"MAINAI-BACKUP-ENC-1\n"
CHUNK_SIZE = 1024 * 1024
NONCE_PREFIX_BYTES = 8
TAG_BYTES = 16
KEY_BYTES = 32


class OffsiteError(RuntimeError):
    """A failed or refused operation. Always exits non-zero."""


# ---------------------------------------------------------------------------------------------
# key handling
# ---------------------------------------------------------------------------------------------


def load_key(key_file: str | None = None) -> bytes:
    """32-byte key from --key-file or MAINAI_BACKUP_ENCRYPTION_KEY (base64, hex, or raw text)."""
    if key_file:
        raw = Path(key_file).read_bytes()
    else:
        raw = (os.environ.get("MAINAI_BACKUP_ENCRYPTION_KEY") or "").encode()
    raw = raw.strip()
    if not raw:
        raise OffsiteError(
            "no backup encryption key: set MAINAI_BACKUP_ENCRYPTION_KEY (32 bytes base64/hex) "
            "or pass --key-file. Refusing to write a plaintext dump off-host."
        )
    # Try base64, then hex, then a 32-byte literal. The first two are the documented forms; the
    # literal form keeps a key file usable by an operator without silently accepting a
    # passphrase (which would need a KDF and a salt, and is deliberately out of scope here).
    try:
        decoded = base64.b64decode(raw, validate=True)
        if len(decoded) == KEY_BYTES:
            return decoded
    except Exception:
        pass
    try:
        decoded = bytes.fromhex(raw.decode())
        if len(decoded) == KEY_BYTES:
            return decoded
    except (ValueError, UnicodeDecodeError):
        pass
    if len(raw) == KEY_BYTES:
        return raw
    raise OffsiteError(
        f"backup key must decode to {KEY_BYTES} bytes (got {len(raw)} bytes of input); "
        "generate one with: openssl rand -base64 32"
    )


# ---------------------------------------------------------------------------------------------
# streaming encryption
# ---------------------------------------------------------------------------------------------


def _aesgcm(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key)


def encrypt_file(src: Path, dst: Path, key: bytes, *, aad: bytes = b"", chunk_size: int = CHUNK_SIZE) -> dict:
    """Encrypt `src` to `dst`. Returns metadata for the transport manifest (no key material)."""
    import os as _os

    plaintext_sha = hashlib.sha256()
    plaintext_bytes = 0
    nonce_prefix = _os.urandom(NONCE_PREFIX_BYTES)
    header = {
        "v": FORMAT_VERSION,
        "alg": "AES-256-GCM",
        "chunk_size": chunk_size,
        "nonce_prefix": base64.b64encode(nonce_prefix).decode(),
        "aad": base64.b64encode(aad).decode(),
        "plaintext_bytes": None,  # filled after the streaming pass
        "plaintext_sha256": None,
    }
    # Two-pass is deliberate: the header (authenticated as AAD for every chunk) must contain the
    # plaintext digest, and streaming without buffering the whole dump means computing it first.
    with src.open("rb") as f:
        for block in iter(lambda: f.read(chunk_size), b""):
            plaintext_sha.update(block)
            plaintext_bytes += len(block)
    header["plaintext_bytes"] = plaintext_bytes
    header["plaintext_sha256"] = plaintext_sha.hexdigest()
    header_bytes = (json.dumps(header, sort_keys=True).encode() + b"\n")
    aead = _aesgcm(key)

    with src.open("rb") as fin, dst.open("wb") as fout:
        fout.write(MAGIC)
        fout.write(header_bytes)
        counter = 0
        for block in iter(lambda: fin.read(chunk_size), b""):
            nonce = nonce_prefix + struct.pack(">I", counter)
            # AAD binds each chunk to the header (so the declared digest cannot be swapped) and
            # to its position (so chunks cannot be reordered or dropped undetected).
            ciphertext = aead.encrypt(nonce, block, header_bytes + struct.pack(">I", counter))
            fout.write(struct.pack(">I", len(ciphertext)))
            fout.write(ciphertext)
            counter += 1
        fout.write(struct.pack(">I", 0))  # end marker; truncation is otherwise valid ciphertext
        fout.flush()
        _os.fsync(fout.fileno())
    dst.chmod(0o600)
    return {
        "algorithm": "AES-256-GCM",
        "chunk_size": chunk_size,
        "plaintext_bytes": plaintext_bytes,
        "plaintext_sha256": header["plaintext_sha256"],
        "ciphertext_bytes": dst.stat().st_size,
    }


def decrypt_file(src: Path, dst: Path, key: bytes, *, aad: bytes = b"") -> dict:
    """Verify and decrypt `src` to `dst`. Raises OffsiteError on any tamper or wrong key."""
    from cryptography.exceptions import InvalidTag

    data = src.read_bytes()
    if not data.startswith(MAGIC):
        raise OffsiteError(f"{src.name}: not a MainAI encrypted backup (bad magic)")
    rest = data[len(MAGIC):]
    newline = rest.find(b"\n")
    if newline < 0:
        raise OffsiteError(f"{src.name}: truncated header")
    header_bytes = rest[: newline + 1]
    try:
        header = json.loads(header_bytes)
    except ValueError as exc:
        raise OffsiteError(f"{src.name}: unreadable header: {exc}") from exc
    if header.get("alg") != "AES-256-GCM" or header.get("v") != FORMAT_VERSION:
        raise OffsiteError(f"{src.name}: unsupported encryption format {header!r}")
    if header.get("aad") != base64.b64encode(aad).decode():
        raise OffsiteError(
            f"{src.name}: artifact was encrypted for a different context (AAD mismatch) — "
            "it is not the backup named by this transport manifest"
        )
    nonce_prefix = base64.b64decode(header["nonce_prefix"])
    chunk_size = int(header["chunk_size"])
    aead = _aesgcm(key)

    body = memoryview(rest[newline + 1 :])
    offset = 0
    counter = 0
    digest = hashlib.sha256()
    total = 0
    with dst.open("wb") as fout:
        while True:
            if offset + 4 > len(body):
                raise OffsiteError(f"{src.name}: truncated chunk header")
            (length,) = struct.unpack(">I", body[offset : offset + 4])
            offset += 4
            if length == 0:
                break
            if offset + length > len(body):
                raise OffsiteError(f"{src.name}: truncated chunk {counter}")
            nonce = nonce_prefix + struct.pack(">I", counter)
            try:
                plaintext = aead.decrypt(
                    nonce, bytes(body[offset : offset + length]), header_bytes + struct.pack(">I", counter)
                )
            except InvalidTag as exc:
                raise OffsiteError(
                    f"{src.name}: authentication failed at chunk {counter} — wrong key or tampered artifact"
                ) from exc
            if len(plaintext) > chunk_size:
                raise OffsiteError(f"{src.name}: chunk {counter} is larger than the declared chunk size")
            fout.write(plaintext)
            digest.update(plaintext)
            total += len(plaintext)
            offset += length
            counter += 1
        if offset != len(body):
            raise OffsiteError(f"{src.name}: trailing bytes after the end marker")
        fout.flush()
        os.fsync(fout.fileno())

    if total != header.get("plaintext_bytes") or digest.hexdigest() != header.get("plaintext_sha256"):
        raise OffsiteError(f"{src.name}: decrypted content does not match the authenticated digest")
    return {
        "plaintext_bytes": total,
        "plaintext_sha256": digest.hexdigest(),
        "chunks": counter,
    }


# ---------------------------------------------------------------------------------------------
# destinations
# ---------------------------------------------------------------------------------------------


class FileDestination:
    """A directory that is not the database host's own disk — mounted remote or test harness."""

    scheme = "file"

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, local: Path, name: str) -> None:
        target = self.root / name
        tmp = target.with_name(target.name + ".part")
        shutil.copyfile(local, tmp)
        tmp.chmod(0o600)
        os.replace(tmp, target)

    def get(self, name: str, local: Path) -> None:
        source = self.root / name
        if not source.is_file():
            raise OffsiteError(f"{name}: not present in {self.root}")
        shutil.copyfile(source, local)

    def list(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_file())

    def delete(self, name: str) -> None:
        (self.root / name).unlink(missing_ok=True)

    def describe(self) -> dict:
        return {"scheme": "file", "root": str(self.root)}


class S3Destination:
    """S3-compatible object storage through the `aws` CLI (credentials stay in its own config)."""

    scheme = "s3"

    def __init__(self, bucket: str, prefix: str):
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _uri(self, name: str) -> str:
        return f"s3://{self.bucket}/{self.prefix}/{name}" if self.prefix else f"s3://{self.bucket}/{name}"

    def _aws(self, *args: str) -> str:
        if shutil.which("aws") is None:
            raise OffsiteError(
                "the `aws` CLI is required for s3:// destinations; install it and configure a "
                "restricted backup identity (write-only to this prefix is sufficient for push)"
            )
        result = subprocess.run(["aws", *args], capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise OffsiteError(f"aws {' '.join(args)} failed: {(result.stderr or '').strip()[-400:]}")
        return result.stdout

    def put(self, local: Path, name: str) -> None:
        self._aws("s3", "cp", "--only-show-errors", str(local), self._uri(name))

    def get(self, name: str, local: Path) -> None:
        self._aws("s3", "cp", "--only-show-errors", self._uri(name), str(local))

    def list(self) -> list[str]:
        raw = self._aws("s3", "ls", f"s3://{self.bucket}/{self.prefix}/" if self.prefix else f"s3://{self.bucket}/")
        names = []
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                names.append(parts[-1])
        return sorted(names)

    def delete(self, name: str) -> None:
        self._aws("s3", "rm", "--only-show-errors", self._uri(name))

    def describe(self) -> dict:
        return {"scheme": "s3", "bucket": self.bucket, "prefix": self.prefix}


def open_destination(url: str):
    parsed = urlparse(url)
    if parsed.scheme == "file":
        if not parsed.path or not parsed.path.startswith("/"):
            raise OffsiteError("file:// destination must have an absolute path")
        return FileDestination(Path(parsed.path))
    if parsed.scheme == "s3":
        if not parsed.netloc:
            raise OffsiteError("s3:// destination must name a bucket")
        return S3Destination(parsed.netloc, parsed.path.lstrip("/"))
    raise OffsiteError(
        f"unsupported destination scheme {parsed.scheme!r}; supported: file://, s3://. "
        "Any other provider can be exposed by mounting it or syncing the file:// directory."
    )


# ---------------------------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------------------------


def send_alert(webhook: str | None, payload: dict) -> None:
    """Best-effort failure alert. Never raises: the backup failure itself is the primary signal."""
    if not webhook:
        return
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        webhook, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(request, timeout=5).read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"offsite_backup: alert webhook failed ({type(exc).__name__})", file=sys.stderr)


# ---------------------------------------------------------------------------------------------
# transport manifest
# ---------------------------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _transport_checksum(payload: dict) -> str:
    body = dict(payload)
    body.pop("transport_checksum", None)
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _remote_name(backup_name: str) -> dict[str, str]:
    return {
        "dump": backup_name,
        "encrypted": f"{backup_name}.enc",
        "encrypted_checksum": f"{backup_name}.enc.sha256",
        "manifest": f"{backup_name}.manifest.json",
        "transport": f"{backup_name}.transport.json",
    }


def _read_transport(destination, name: str, workdir: Path) -> dict:
    local = workdir / f"{name}.transport.json"
    destination.get(name, local)
    payload = json.loads(local.read_text(encoding="utf-8"))
    declared = payload.get("transport_checksum")
    if not declared or declared != _transport_checksum(payload):
        raise OffsiteError(f"{name}: transport manifest checksum mismatch — refusing to trust it")
    return payload


# ---------------------------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------------------------


def push(
    database_url: str,
    destination,
    key: bytes,
    *,
    keep: int = 7,
    workdir: Path | None = None,
) -> dict:
    if keep < 1:
        raise OffsiteError("--keep must be at least 1; refusing to push a backup it would immediately prune")
    created_at = datetime.now(timezone.utc)
    tmp = Path(workdir or tempfile.mkdtemp(prefix="mainai-offsite-"))
    tmp.mkdir(parents=True, exist_ok=True)
    db_name = urlparse(database_url).path.lstrip("/")
    backup_name = f"{db_name}-{created_at.strftime('%Y%m%dT%H%M%SZ')}.dump"
    dump_path = tmp / backup_name

    manifest = db_recovery.backup(database_url, dump_path)
    verification_errors = db_recovery.verify(dump_path)
    if verification_errors:
        raise OffsiteError("the local backup is not verifiable, refusing to upload it: " + "; ".join(verification_errors))

    artifacts = _remote_name(backup_name)
    aad = artifacts["encrypted"].encode()
    encryption = encrypt_file(dump_path, tmp / artifacts["encrypted"], key, aad=aad)
    enc_sha = _sha256_file(tmp / artifacts["encrypted"])
    (tmp / artifacts["encrypted_checksum"]).write_text(
        f"{enc_sha}  {artifacts['encrypted']}\n", encoding="utf-8"
    )

    # db_recovery.backup already wrote `<backup_name>.manifest.json` next to the dump; the
    # upload loop below reads it from there. Copying it to the same path was a no-op that
    # shutil correctly rejected as SameFileError.
    transport = {
        "format_version": FORMAT_VERSION,
        "tool": TOOL,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "source": {
            "database": db_name,
            "host": urlparse(database_url).hostname,
            "alembic_revision": manifest["alembic"]["current_revision"],
            "dump_sha256": manifest["dump"]["sha256"],
        },
        "artifacts": {
            "dump": {"name": artifacts["dump"], "bytes": dump_path.stat().st_size, "sha256": manifest["dump"]["sha256"]},
            "encrypted": {"name": artifacts["encrypted"], "bytes": (tmp / artifacts["encrypted"]).stat().st_size, "sha256": enc_sha},
            "manifest": {"name": artifacts["manifest"], "sha256": _sha256_file(tmp / artifacts["manifest"])},
            "checksum": {"name": artifacts["encrypted_checksum"], "sha256": _sha256_file(tmp / artifacts["encrypted_checksum"])},
        },
        "encryption": {k: encryption[k] for k in ("algorithm", "chunk_size", "plaintext_bytes", "plaintext_sha256")},
        "retention": {"keep": keep},
        "destination": destination.describe(),
        "off_host_verified": False,
        "note": "This artifact left the database disk. Off-host retention is only proven once a copy "
        "is stored where host loss cannot remove it and has been restored from that copy.",
    }
    transport["transport_checksum"] = _transport_checksum(transport)
    (tmp / artifacts["transport"]).write_text(json.dumps(transport, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Upload order is a commit protocol: the transport manifest is the marker that a complete
    # backup exists. A crash before it lands leaves orphan artifacts that prune ignores, never a
    # manifest pointing at a missing file.
    for name in (artifacts["encrypted"], artifacts["encrypted_checksum"], artifacts["manifest"], artifacts["transport"]):
        destination.put(tmp / name, name)

    pruned = prune(destination, keep=keep, workdir=tmp)
    return {
        "name": backup_name,
        "artifacts": artifacts,
        "bytes_encrypted": (tmp / artifacts["encrypted"]).stat().st_size,
        "encryption": transport["encryption"],
        "destination": destination.describe(),
        "pruned": pruned,
        "off_host_verified": False,
    }


# ---------------------------------------------------------------------------------------------
# verify / fetch / restore
# ---------------------------------------------------------------------------------------------


def verify_remote(destination, name: str, workdir: Path | None = None) -> dict:
    tmp = Path(workdir or tempfile.mkdtemp(prefix="mainai-offsite-verify-"))
    tmp.mkdir(parents=True, exist_ok=True)
    transport = _read_transport(destination, _remote_name(name)["transport"], tmp)
    artifacts = transport["artifacts"]
    enc_local = tmp / artifacts["encrypted"]["name"]
    destination.get(artifacts["encrypted"]["name"], enc_local)
    actual = _sha256_file(enc_local)
    if actual != artifacts["encrypted"]["sha256"]:
        raise OffsiteError(
            f"{name}: encrypted artifact checksum mismatch (declared {artifacts['encrypted']['sha256']}, got {actual})"
        )
    manifest_local = tmp / artifacts["manifest"]["name"]
    destination.get(artifacts["manifest"]["name"], manifest_local)
    if _sha256_file(manifest_local) != artifacts["manifest"]["sha256"]:
        raise OffsiteError(f"{name}: recovery manifest checksum mismatch")
    return {
        "name": name,
        "encrypted_sha256": actual,
        "bytes": enc_local.stat().st_size,
        "alembic_revision": transport["source"]["alembic_revision"],
        "encryption": transport["encryption"],
        "created_at": transport["created_at"],
    }


def fetch_and_decrypt(destination, name: str, key: bytes, workdir: Path) -> tuple[Path, dict]:
    artifacts = _remote_name(name)
    transport = _read_transport(destination, artifacts["transport"], workdir)
    expected = transport["artifacts"]
    enc_path = workdir / artifacts["encrypted"]
    destination.get(artifacts["encrypted"], enc_path)
    if _sha256_file(enc_path) != expected["encrypted"]["sha256"]:
        raise OffsiteError(f"{name}: downloaded artifact does not match its recorded checksum")
    manifest_path = workdir / artifacts["manifest"]
    destination.get(artifacts["manifest"], manifest_path)
    if _sha256_file(manifest_path) != expected["manifest"]["sha256"]:
        raise OffsiteError(f"{name}: downloaded recovery manifest does not match its recorded checksum")

    dump_path = workdir / artifacts["dump"]
    decrypt_file(enc_path, dump_path, key, aad=artifacts["encrypted"].encode())
    actual_dump_sha = _sha256_file(dump_path)
    if actual_dump_sha != expected["dump"]["sha256"]:
        raise OffsiteError(f"{name}: decrypted dump does not match the checksum in the transport manifest")
    # `db_recovery.verify()` checks the dump against its sibling .sha256 file. That file is
    # deliberately not uploaded (the encrypted artifact is the only copy off-host), so recreate
    # it from the authenticated digest rather than weakening the verifier.
    db_recovery._write_checksum_file(dump_path)
    errors = db_recovery.verify(dump_path)
    if errors:
        raise OffsiteError(f"{name}: restored-copy verification failed: " + "; ".join(errors))
    return dump_path, transport


def restore(destination, name: str, key: bytes, target_url: str, *, confirmed: bool) -> dict:
    if not confirmed:
        raise OffsiteError("refusing to restore without --confirm")
    workdir = Path(tempfile.mkdtemp(prefix="mainai-offsite-restore-"))
    dump_path, transport = fetch_and_decrypt(destination, name, key, workdir)
    errors = db_recovery.restore(dump_path, target_url, confirmed=True)
    if errors:
        raise OffsiteError(f"{name}: restore completed but verification found differences: " + "; ".join(errors))
    return {
        "name": name,
        "target": urlparse(target_url).path.lstrip("/"),
        "alembic_revision": transport["source"]["alembic_revision"],
        "verification_errors": errors,
    }


# ---------------------------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------------------------


def prune(destination, *, keep: int, workdir: Path | None = None) -> list[str]:
    """Delete complete backups older than the newest `keep`. Never touches unmanaged files."""
    if keep < 1:
        raise OffsiteError("--keep must be at least 1")
    tmp = Path(workdir or tempfile.mkdtemp(prefix="mainai-offsite-prune-"))
    tmp.mkdir(parents=True, exist_ok=True)
    transport_names = [n for n in destination.list() if n.endswith(".transport.json")]
    records = []
    for name in transport_names:
        payload = _read_transport(destination, name, tmp)
        records.append((payload["created_at"], name, payload))
    records.sort(key=lambda item: item[0], reverse=True)

    removed: list[str] = []
    for _, transport_name, payload in records[keep:]:
        # Only files named by a valid transport manifest are deleted, and the manifest itself
        # goes last: an interrupted prune can leave orphans (invisible to retention) but never
        # a manifest pointing at a deleted artifact.
        artifact_names = [a["name"] for a in payload.get("artifacts", {}).values() if a.get("name")]
        for filename in artifact_names:
            destination.delete(filename)
            removed.append(filename)
        destination.delete(transport_name)
        removed.append(transport_name)
    return removed


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_push = sub.add_parser("push", help="create, verify, encrypt and upload a backup")
    p_push.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    p_push.add_argument("--destination", required=True)
    p_push.add_argument("--keep", type=int, default=7)

    p_verify = sub.add_parser("verify", help="check a remote artifact's integrity")
    p_verify.add_argument("--destination", required=True)
    p_verify.add_argument("--name", required=True)

    p_restore = sub.add_parser("restore", help="download, decrypt, verify and restore")
    p_restore.add_argument("--destination", required=True)
    p_restore.add_argument("--name", required=True)
    p_restore.add_argument("--target-url", required=True)
    p_restore.add_argument("--confirm", action="store_true")

    p_prune = sub.add_parser("prune", help="apply retention to complete backups")
    p_prune.add_argument("--destination", required=True)
    p_prune.add_argument("--keep", type=int, default=7)

    for p in (p_push, p_verify, p_restore, p_prune):
        p.add_argument("--key-file")
        p.add_argument("--alert-webhook", default=os.environ.get("MAINAI_BACKUP_ALERT_WEBHOOK"))

    args = parser.parse_args(argv)
    started = datetime.now(timezone.utc)

    try:
        destination = open_destination(args.destination)
        if args.command == "push":
            if not args.database_url:
                raise OffsiteError("--database-url (or DATABASE_URL) is required")
            result = push(args.database_url, destination, load_key(args.key_file), keep=args.keep)
        elif args.command == "verify":
            result = verify_remote(destination, args.name)
        elif args.command == "restore":
            result = restore(destination, args.name, load_key(args.key_file), args.target_url, confirmed=args.confirm)
        else:
            result = {"pruned": prune(destination, keep=args.keep)}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except OffsiteError as exc:
        payload = {
            "tool": TOOL,
            "event": "offsite_backup_failed",
            "command": args.command,
            "at": started.isoformat().replace("+00:00", "Z"),
            "error": str(exc),
        }
        send_alert(getattr(args, "alert_webhook", None), payload)
        print(f"FAILED [{args.command}]: {exc}", file=sys.stderr)
        return 2 if "refusing" in str(exc).lower() or "no backup encryption key" in str(exc) else 1
    except Exception as exc:  # noqa: BLE001 — alert on anything unexpected, never fail silently
        send_alert(
            getattr(args, "alert_webhook", None),
            {
                "tool": TOOL,
                "event": "offsite_backup_failed",
                "command": args.command,
                "at": started.isoformat().replace("+00:00", "Z"),
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        print(f"FAILED [{args.command}]: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
