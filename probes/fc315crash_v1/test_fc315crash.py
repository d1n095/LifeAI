"""#315 crash-consistency & finalized-file integrity — independent examiner suite.

Subject: #315 head SHA 0741a3d5 (docs-only child of product SHA 292a091; resumable store tree
byte-identical). Investigates the O-1 observation from fc315http_v1 (VERDICT_FC315HTTP_0741a3d.md):
does manifest.offset ever diverge from the actual spool length through REALISTIC crash/recovery
rather than only out-of-band tampering?

Root facts (read-verified in app/large_file_lane/session.py @ 0741a3d5):
  write_part(): append+FLUSH to spool, THEN manifest.offset+=, THEN _persist() (atomic rename).
    - No fsync anywhere (no durability confirmation): flushed spool bytes survive a process KILL,
      but the not-yet-written manifest does not.
    - On resume the spool is opened "ab" and NEVER reconciled/truncated to manifest.offset.
  finalize(): hashes the WHOLE spool to EOF (not bounded by offset/declared_total) and gates
    completeness on manifest.offset only (never re-stats the spool length).

Fault injection + isolated tmp fixtures. Store level (NOT re-running HTTP auth/quota/normal
restart, already covered by fc315http_v1). No DB, no network, no paid calls. first_cycle_go=false.

CC1 is the reproducible FAILING test for Cursor (strict xfail: asserts the integrity invariant
that must hold; flips to a red XPASS when the product is fixed). CC2/CC3/CC4/CC7 are PASS
evidence tests that pin the exact broken behaviour so it cannot change silently.
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid

import pytest

from _guards import _no_egress  # noqa: F401  (autouse egress guard)

PART = 64 * 1024
OWNER = uuid.uuid4()


def _store(tmp_path, **kw):
    from app.large_file_lane.http_store import ResumableHttpStore

    return ResumableHttpStore(root=tmp_path / "store", **kw)


def _spool(st, sid):
    from app.large_file_lane.session import SPOOL

    return st.session_root(OWNER, sid) / SPOOL


def _manifest_path(st, sid):
    from app.large_file_lane.session import MANIFEST

    return st.session_root(OWNER, sid) / MANIFEST


def _mk(st, nparts):
    """A clean session with `nparts` distinct 64 KiB parts; returns (sid, parts, intended)."""
    parts = [bytes([i % 251 + 1]) * PART for i in range(nparts)]
    intended = b"".join(parts)
    sid = uuid.UUID(st.create(OWNER, declared_total_bytes=len(intended))["session_id"])
    return sid, parts, intended


def _resume_as_correct_client(st, sid, parts):
    """A CORRECT resumable client: ask where to resume, then send the remaining declared bytes
    from that byte offset. (The store exposes only a byte offset; the client maps it to parts.)"""
    off = st.get(OWNER, sid)["offset"]
    assert off % PART == 0, "fixtures use whole 64 KiB parts"
    for p in parts[off // PART:]:
        st.write_part(OWNER, sid, p)


# ---------------------------------------------------------------------------
# CC1 — REPRODUCIBLE FAILING TEST FOR CURSOR (strict xfail): finalized-file integrity invariant
# ---------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason="CC1/O-1 P2: a process kill between the spool append "
                   "(flushed) and the manifest persist, followed by a CORRECT client resume, "
                   "yields a finalized file longer than declared_total with a duplicated chunk, "
                   "and finalize's sha256 does not match the intended content. write_part opens "
                   "'ab' and never truncates the spool to manifest.offset on resume; finalize "
                   "hashes the whole spool and never checks spool length == declared_total.")
def test_CC1_finalized_file_matches_declared_after_crash_resume(tmp_path):
    st = _store(tmp_path)
    sid, parts, intended = _mk(st, 4)
    st.write_part(OWNER, sid, parts[0])
    st.write_part(OWNER, sid, parts[1])

    # process KILL between spool append(flushed) and manifest persist: part[2] reached the spool
    # and was flushed to the OS, but the manifest replace never happened.
    with _spool(st, sid).open("ab") as h:
        h.write(parts[2])
        h.flush()

    # fresh process + correct client resume to declared_total
    st2 = _store(tmp_path)
    _resume_as_correct_client(st2, sid, parts)
    fin = st2.finalize(OWNER, sid)

    # INVARIANT the product must guarantee (currently violated -> strict xfail):
    assert _spool(st2, sid).stat().st_size == len(intended), "finalized file length != declared_total"
    assert fin["sha256"] == hashlib.sha256(intended).hexdigest(), "finalized checksum != intended content"


# ---------------------------------------------------------------------------
# CC2 — EVIDENCE (PASS): pin the exact corrupt terminal state O-1 produces
# ---------------------------------------------------------------------------
def test_CC2_crash_resume_produces_overlong_duplicated_file_reported_complete(tmp_path):
    st = _store(tmp_path)
    sid, parts, intended = _mk(st, 4)
    st.write_part(OWNER, sid, parts[0])
    st.write_part(OWNER, sid, parts[1])
    with _spool(st, sid).open("ab") as h:                 # orphaned flushed append (kill before persist)
        h.write(parts[2]); h.flush()

    st2 = _store(tmp_path)
    _resume_as_correct_client(st2, sid, parts)
    fin = st2.finalize(OWNER, sid)
    on_disk = _spool(st2, sid).read_bytes()

    # client-visible status falsely reports a complete, consistent, stored file ...
    assert fin["outcome"] == "stored" and fin["finalized"] is True and fin["complete"] is True
    assert fin["offset"] == len(intended)                 # manifest accounting says exactly declared
    # ... while the actual stored file is LONGER than declared and contains a duplicated chunk
    assert len(on_disk) == len(intended) + PART
    assert on_disk[2 * PART:3 * PART] == on_disk[3 * PART:4 * PART] == parts[2]  # part[2] stored twice
    # ... and the reported checksum is of the corrupted bytes, not the intended content
    assert fin["sha256"] == hashlib.sha256(on_disk).hexdigest()
    assert fin["sha256"] != hashlib.sha256(intended).hexdigest()


# ---------------------------------------------------------------------------
# CC3 — EVIDENCE (PASS): a TORN (partial) persisted write corrupts at an arbitrary offset
# ---------------------------------------------------------------------------
def test_CC3_torn_partial_spool_write_corrupts_arbitrary_offset(tmp_path):
    st = _store(tmp_path)
    sid, parts, intended = _mk(st, 3)
    st.write_part(OWNER, sid, parts[0])
    torn = 20_000  # a partial tail of part[1] made it to the spool before the kill
    with _spool(st, sid).open("ab") as h:
        h.write(parts[1][:torn]); h.flush()

    st2 = _store(tmp_path)
    _resume_as_correct_client(st2, sid, parts)            # resumes at PART, re-sends full part[1], part[2]
    fin = st2.finalize(OWNER, sid)

    assert fin["outcome"] == "stored" and fin["complete"] is True   # still reported complete
    assert _spool(st2, sid).stat().st_size == len(intended) + torn  # file is `torn` bytes too long
    assert fin["sha256"] != hashlib.sha256(intended).hexdigest()


# ---------------------------------------------------------------------------
# CC4 — EVIDENCE (PASS): a declared-complete session can finalize FEWER bytes than declared
# ---------------------------------------------------------------------------
def test_CC4_declared_complete_can_finalize_fewer_bytes(tmp_path):
    """Durability/no-fsync direction: manifest persisted offset==declared, but the last flushed
    spool chunk was lost (power loss / lost page-cache). finalize gates on manifest.offset and
    reads the spool to EOF, so it reports stored/complete with a file SHORTER than declared."""
    st = _store(tmp_path)
    sid, parts, intended = _mk(st, 4)
    for p in parts:
        st.write_part(OWNER, sid, p)                      # manifest.offset == declared_total (complete)
    full = _spool(st, sid).read_bytes()
    _spool(st, sid).write_bytes(full[:-PART])             # lose the last flushed chunk; manifest intact

    st2 = _store(tmp_path)
    assert st2.get(OWNER, sid)["complete"] is True        # manifest still claims complete
    fin = st2.finalize(OWNER, sid)
    assert fin["outcome"] == "stored" and fin["complete"] is True and fin["offset"] == len(intended)
    assert _spool(st2, sid).stat().st_size == len(intended) - PART         # actually short
    assert fin["sha256"] == hashlib.sha256(full[:-PART]).hexdigest()       # checksum over fewer bytes


# ---------------------------------------------------------------------------
# CC5 — manifest corruption + fresh process: fail-closed (not presented as valid)
# ---------------------------------------------------------------------------
def test_CC5_corrupt_manifest_is_fail_closed_on_recovery(tmp_path):
    from app.large_file_lane.http_store import ResumableHttpError

    st = _store(tmp_path)
    sid, parts, _ = _mk(st, 2)
    st.write_part(OWNER, sid, parts[0])
    _manifest_path(st, sid).write_text("{ this is not valid json")   # corrupt the manifest

    st2 = _store(tmp_path)
    # get()/finalize() must NOT return a 'stored'/valid row for a corrupt manifest; they raise.
    with pytest.raises((ResumableHttpError, json.JSONDecodeError, ValueError, TypeError)):
        st2.get(OWNER, sid)
    with pytest.raises((ResumableHttpError, json.JSONDecodeError, ValueError, TypeError)):
        st2.finalize(OWNER, sid)
    # reserved_bytes() swallows the corrupt manifest (does not crash); documents quota
    # under-accounting for a corrupt session (minor) rather than over-counting.
    assert st2.reserved_bytes(OWNER) == 0


# ---------------------------------------------------------------------------
# CC6 — concurrent finalize vs resume-write against the same session (flock)
# ---------------------------------------------------------------------------
def test_CC6_concurrent_finalize_and_resume_write_leave_coherent_state(tmp_path):
    st = _store(tmp_path)
    sid, parts, intended = _mk(st, 4)
    for p in parts:                                       # fully received (eligible to finalize)
        st.write_part(OWNER, sid, p)

    outcomes: dict[str, object] = {}
    lock = threading.Lock()

    def finalize():
        try:
            r = _store(tmp_path).finalize(OWNER, sid)
            with lock:
                outcomes["finalize"] = r["outcome"]
        except Exception as e:  # noqa: BLE001
            with lock:
                outcomes["finalize_err"] = type(e).__name__

    def late_write():
        try:
            r = _store(tmp_path).write_part(OWNER, sid, parts[0])  # a straggler part after completion
            with lock:
                outcomes["write"] = r["outcome"]
        except Exception as e:  # noqa: BLE001
            with lock:
                outcomes["write_err"] = type(e).__name__

    ts = [threading.Thread(target=finalize), threading.Thread(target=late_write)]
    [t.start() for t in ts]; [t.join() for t in ts]

    # The per-session flock serialized the two operations: the manifest on disk is intact and
    # parses, and the terminal state is coherent (the straggler is either rejected as
    # over-declared or as already-finalized; it is never silently appended past declared_total).
    manifest = json.loads(_manifest_path(st, sid).read_text())
    assert manifest["offset"] == len(intended)            # never advanced past declared
    assert outcomes.get("write") in (None, "rejected") or "write_err" in outcomes, outcomes
    reloaded = _store(tmp_path).get(OWNER, sid)
    assert reloaded["offset"] == len(intended)


# ---------------------------------------------------------------------------
# CC7 — TRUST BOUNDARY (PASS): corrupted content is presentable downstream as valid stored content
# ---------------------------------------------------------------------------
def test_CC7_corrupted_file_is_presentable_as_valid_stored_content(tmp_path):
    """Item 7 conclusion. After the CC1 crash path, the finalized session advertises exactly the
    fields a downstream consumer would trust — stored/finalized/complete + a sha256 — and public()
    exposes NO field that reveals the length divergence (no verified spool length, and no
    client-declared expected checksum is accepted at create to verify against). So an incomplete/
    corrupted file IS presentable downstream as valid stored content."""
    st = _store(tmp_path)
    sid, parts, intended = _mk(st, 4)
    st.write_part(OWNER, sid, parts[0]); st.write_part(OWNER, sid, parts[1])
    with _spool(st, sid).open("ab") as h:
        h.write(parts[2]); h.flush()
    st2 = _store(tmp_path)
    _resume_as_correct_client(st2, sid, parts)
    fin = st2.finalize(OWNER, sid)

    # downstream-visible "valid stored content" contract is fully satisfied despite corruption
    assert fin["stored"] is True and fin["finalized"] is True and fin["complete"] is True
    assert isinstance(fin["sha256"], str) and len(fin["sha256"]) == 64
    # the public row carries declared_total_bytes and offset, but NO field exposing the actual
    # stored byte length, so a consumer cannot detect len(file)=declared+PART from the row alone.
    row = st2.get(OWNER, sid)
    assert "declared_total_bytes" in row and row["offset"] == len(intended)
    length_fields = [k for k in row if "actual" in k.lower() or "spool" in k.lower() or "verified_len" in k.lower()]
    assert length_fields == [], f"unexpected length-verification field(s) present: {length_fields}"
