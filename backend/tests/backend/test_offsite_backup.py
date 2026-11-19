"""Off-site backup: encryption, tamper detection, retention, destinations and alerts.

These are unit tests over the artifact pipeline. They do not use PostgreSQL: the real
backup -> encrypt -> upload -> download -> decrypt -> restore cycle is exercised separately
against the live acceptance database, because that is the only place a claim about restorability
belongs.

What matters here is the failure side: a corrupted or truncated artifact must never decrypt into
something that is then trusted, retention must never delete a file it did not create, and a
missing key must stop the upload rather than writing plaintext off-host.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = BACKEND_ROOT / "scripts" / "ops" / "offsite_backup.py"


def _load():
    spec = importlib.util.spec_from_file_location("offsite_backup", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def offsite(monkeypatch, tmp_path):
    module = _load()
    monkeypatch.setenv("MAINAI_BACKUP_ENCRYPTION_KEY", _b64_key(module, b"k" * 32))
    return module


def _b64_key(module, key: bytes) -> str:
    import base64

    return base64.b64encode(key).decode()


def test_encryption_roundtrip_is_authenticated_and_streaming(offsite, tmp_path):
    plaintext = b"founder memory\n" * 200_000  # multiple 1 MiB chunks
    src = tmp_path / "dump.bin"
    src.write_bytes(plaintext)
    enc = tmp_path / "dump.bin.enc"
    out = tmp_path / "dump.bin.out"

    key = offsite.load_key()
    metadata = offsite.encrypt_file(src, enc, key, aad=b"dump.bin.enc", chunk_size=64 * 1024)
    assert metadata["plaintext_sha256"] == __import__("hashlib").sha256(plaintext).hexdigest()
    assert enc.read_bytes().startswith(offsite.MAGIC)
    assert plaintext[:100] not in enc.read_bytes()

    result = offsite.decrypt_file(enc, out, key, aad=b"dump.bin.enc")
    assert out.read_bytes() == plaintext
    assert result["plaintext_sha256"] == metadata["plaintext_sha256"]


def test_a_single_flipped_byte_fails_authentication(offsite, tmp_path):
    src = tmp_path / "dump.bin"
    src.write_bytes(b"row data" * 10_000)
    enc = tmp_path / "dump.bin.enc"
    key = offsite.load_key()
    offsite.encrypt_file(src, enc, key, aad=b"x")

    data = bytearray(enc.read_bytes())
    data[len(data) // 2] ^= 0x01
    enc.write_bytes(bytes(data))

    with pytest.raises(offsite.OffsiteError, match="authentication failed"):
        offsite.decrypt_file(enc, tmp_path / "out", key, aad=b"x")


def test_a_wrong_key_never_produces_output(offsite, tmp_path):
    src = tmp_path / "dump.bin"
    src.write_bytes(b"secret")
    enc = tmp_path / "dump.bin.enc"
    offsite.encrypt_file(src, enc, offsite.load_key(), aad=b"x")

    with pytest.raises(offsite.OffsiteError, match="authentication failed"):
        offsite.decrypt_file(enc, tmp_path / "out", b"z" * 32, aad=b"x")
    assert not (tmp_path / "out").exists() or (tmp_path / "out").stat().st_size == 0


def test_a_truncated_artifact_is_refused(offsite, tmp_path):
    src = tmp_path / "dump.bin"
    src.write_bytes(b"row data" * 100_000)
    enc = tmp_path / "dump.bin.enc"
    key = offsite.load_key()
    offsite.encrypt_file(src, enc, key, aad=b"x", chunk_size=16 * 1024)

    truncated = enc.read_bytes()[:-100]
    enc.write_bytes(truncated)

    with pytest.raises(offsite.OffsiteError, match="truncated|authentication|digest"):
        offsite.decrypt_file(enc, tmp_path / "out", key, aad=b"x")


def test_an_artifact_encrypted_for_a_different_backup_is_refused(offsite, tmp_path):
    src = tmp_path / "a.dump"
    src.write_bytes(b"data")
    enc = tmp_path / "a.dump.enc"
    key = offsite.load_key()
    offsite.encrypt_file(src, enc, key, aad=b"a.dump.enc")

    with pytest.raises(offsite.OffsiteError, match="different context"):
        offsite.decrypt_file(enc, tmp_path / "out", key, aad=b"b.dump.enc")


def test_missing_key_refuses_to_upload_plaintext(offsite, monkeypatch):
    monkeypatch.delenv("MAINAI_BACKUP_ENCRYPTION_KEY")
    with pytest.raises(offsite.OffsiteError, match="no backup encryption key"):
        offsite.load_key()


def test_only_file_and_s3_destinations_are_accepted(offsite, tmp_path):
    assert offsite.open_destination(f"file://{tmp_path}").scheme == "file"
    assert offsite.open_destination("s3://bucket/prefix").scheme == "s3"
    with pytest.raises(offsite.OffsiteError, match="unsupported destination scheme"):
        offsite.open_destination("https://example.com/backups")
    with pytest.raises(offsite.OffsiteError, match="absolute path"):
        offsite.open_destination("file://relative")


def test_retention_keeps_newest_and_never_deletes_an_unmanaged_file(offsite, tmp_path):
    destination = offsite.FileDestination(tmp_path)
    (tmp_path / "operator-notes.txt").write_text("not a backup")

    for index, stamp in enumerate(
        ("2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z", "2026-11-03T00:00:00Z")
    ):
        name = f"db-{index}.dump"
        artifacts = {
            "dump": {"name": name, "sha256": "0" * 64},
            "encrypted": {"name": f"{name}.enc", "sha256": "1" * 64},
            "manifest": {"name": f"{name}.manifest.json", "sha256": "2" * 64},
            "checksum": {"name": f"{name}.enc.sha256", "sha256": "3" * 64},
        }
        for artifact in artifacts.values():
            (tmp_path / artifact["name"]).write_text("x")
        payload = {
            "format_version": 1,
            "tool": "mainai-offsite-backup",
            "created_at": stamp,
            "artifacts": artifacts,
        }
        payload["transport_checksum"] = offsite._transport_checksum(payload)
        (tmp_path / f"{name}.transport.json").write_text(json.dumps(payload))

    removed = offsite.prune(destination, keep=2)

    assert "db-0.dump.enc" in removed
    assert "db-1.dump.enc" not in removed
    assert "db-2.dump.enc" not in removed
    assert (tmp_path / "operator-notes.txt").exists()
    assert (tmp_path / "db-2.dump.enc").exists()
    assert not (tmp_path / "db-0.dump.enc").exists()


def test_a_tampered_transport_manifest_is_refused(offsite, tmp_path):
    destination = offsite.FileDestination(tmp_path)
    payload = {"format_version": 1, "created_at": "2026-11-01T00:00:00Z", "artifacts": {}}
    payload["transport_checksum"] = offsite._transport_checksum(payload)
    (tmp_path / "db.dump.transport.json").write_text(json.dumps(payload))
    payload["created_at"] = "2026-11-02T00:00:00Z"
    (tmp_path / "db.dump.transport.json").write_text(json.dumps(payload))

    with pytest.raises(offsite.OffsiteError, match="checksum mismatch"):
        offsite._read_transport(destination, "db.dump.transport.json", tmp_path)


class _AlertHandler(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        _AlertHandler.received.append(json.loads(self.rfile.read(length)))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        return


def test_a_failed_run_emits_an_alert(offsite, monkeypatch):
    _AlertHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AlertHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.delenv("MAINAI_BACKUP_ENCRYPTION_KEY")
        code = offsite.main(
            [
                "push",
                "--database-url",
                "postgresql://x@localhost/db",
                "--destination",
                f"file://{Path(os.environ.get('TMPDIR', '/tmp'))}",
                "--alert-webhook",
                f"http://127.0.0.1:{server.server_address[1]}/hook",
            ]
        )
    finally:
        server.shutdown()
        server.server_close()

    assert code != 0
    assert _AlertHandler.received
    assert _AlertHandler.received[0]["event"] == "offsite_backup_failed"
    assert "encryption key" in _AlertHandler.received[0]["error"]


def test_restore_requires_confirmation(offsite, tmp_path):
    destination = offsite.FileDestination(tmp_path)
    with pytest.raises(offsite.OffsiteError, match="without --confirm"):
        offsite.restore(destination, "db.dump", offsite.load_key(), "postgresql://x/y", confirmed=False)
