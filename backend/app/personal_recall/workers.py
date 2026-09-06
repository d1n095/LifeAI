"""Opt-in durable invalidation worker; no scheduler or canonical write hooks installed.

Events contain identifiers, never content. A fresh canonical source read replaces only that
source's projection. Queue acknowledgement and projection commit are one SQLite transaction.
The private local database is disposable derived state, not a canonical outbox.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.personal_recall.index import MAX_SNAPSHOT_BYTES, RecallIndexError, SnapshotStoragePolicy, _decode, _encode
from app.personal_recall.snapshot_protection import RecallSnapshotProtector


class ChangeKind(str, Enum):
    NEW_MESSAGE = "new_message"
    EDITED_MESSAGE = "edited_message"
    NEW_DOCUMENT = "new_document"
    DELETED_DOCUMENT = "deleted_document"
    MEMORY_REVOKE = "memory_revoke"
    MEMORY_PURGE = "memory_purge"
    KNOWLEDGE_SUPERSESSION = "knowledge_supersession"

    @property
    def family(self):
        if self in (self.NEW_MESSAGE, self.EDITED_MESSAGE):
            return "message"
        if self in (self.MEMORY_REVOKE, self.MEMORY_PURGE):
            return "memory"
        return "document"


@dataclass(frozen=True)
class SourceChange:
    event_id: str
    owner_id: str
    source_id: str
    kind: ChangeKind
    canonical_version: int = 1
    event_type: str = "updated"
    version_fenced: bool = False


class RecallIndexWorker:
    def __init__(self, path: Path, *, policy: SnapshotStoragePolicy, owner_id: str,
                 protector: RecallSnapshotProtector, authorize, load_source, load_generation=None, test_only=False):
        if not owner_id or not callable(authorize) or not callable(load_source):
            raise RecallIndexError("explicit owner, authority gate and canonical loader required")
        if not test_only:
            # No reviewed production key hierarchy exists in this branch. An arbitrary
            # object implementing seal/open must not silently enable production storage.
            raise RecallIndexError("production snapshot key hierarchy is not integrated")
        self.path = policy.resolve(path)
        if not self.path.parent.is_dir():
            raise RecallIndexError("worker directory must already exist")
        self.owner_id, self.protector = owner_id, protector
        self.authorize, self.load_source = authorize, load_source
        self.load_generation = load_generation or getattr(load_source, "canonical_generation", None)
        # Private directory required: SQLite creates journals alongside the database.
        if self.path.parent.stat().st_mode & 0o077:
            raise RecallIndexError("worker directory must be private (0700)")
        with self._connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS identity (owner TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    source TEXT NOT NULL, kind TEXT NOT NULL, canonical_version INTEGER NOT NULL DEFAULT 1,
                    event_type TEXT NOT NULL DEFAULT 'updated', version_fenced INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS projections (
                    family TEXT NOT NULL, source TEXT NOT NULL, seq INTEGER NOT NULL, canonical_version INTEGER NOT NULL DEFAULT 1,
                    payload BLOB NOT NULL, PRIMARY KEY(family, source));
            ''')
            columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
            if "canonical_version" not in columns:
                db.execute("ALTER TABLE events ADD COLUMN canonical_version INTEGER NOT NULL DEFAULT 1")
            if "event_type" not in columns:
                db.execute("ALTER TABLE events ADD COLUMN event_type TEXT NOT NULL DEFAULT 'updated'")
            if "version_fenced" not in columns:
                db.execute("ALTER TABLE events ADD COLUMN version_fenced INTEGER NOT NULL DEFAULT 0")
            columns = {row[1] for row in db.execute("PRAGMA table_info(projections)")}
            if "canonical_version" not in columns:
                db.execute("ALTER TABLE projections ADD COLUMN canonical_version INTEGER NOT NULL DEFAULT 1")
            with self._transaction(db):
                row = db.execute("SELECT owner FROM identity").fetchone()
                if row is None:
                    db.execute("INSERT INTO identity VALUES (?)", (owner_id,))
                elif row[0] != owner_id:
                    raise RecallIndexError("worker database owner mismatch")

    @contextmanager
    def _connection(self):
        if self.path.is_symlink():
            raise RecallIndexError("worker database cannot be a symlink")
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA secure_delete=ON")
            yield db
        finally:
            db.close()

    @staticmethod
    @contextmanager
    def _transaction(db):
        db.execute("BEGIN IMMEDIATE")
        try:
            yield
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def _gate(self):
        if self.authorize(self.owner_id) is not True:
            raise PermissionError("worker authority missing or revoked")

    def enqueue(self, change: SourceChange):
        self._gate()
        if change.owner_id != self.owner_id or not isinstance(change.kind, ChangeKind):
            raise RecallIndexError("invalid event owner or kind")
        if change.canonical_version < 1 or any(not isinstance(v, str) or not v or len(v) > 256 for v in (change.event_id, change.source_id)):
            raise RecallIndexError("invalid event identifier")
        with self._connection() as db, self._transaction(db):
            prior = db.execute("SELECT source, kind, canonical_version, event_type, version_fenced FROM events WHERE event_id=?", (change.event_id,)).fetchone()
            identity = (change.source_id, change.kind.value, change.canonical_version, change.event_type, int(change.version_fenced))
            if prior and prior != identity:
                raise RecallIndexError("event identity collision")
            db.execute("INSERT OR IGNORE INTO events(event_id, source, kind, canonical_version, event_type, version_fenced) VALUES (?, ?, ?, ?, ?, ?)",
                       (change.event_id, change.source_id, change.kind.value, change.canonical_version, change.event_type, int(change.version_fenced)))

    def _aad(self, family, source, seq, canonical_version=1):
        return json.dumps(["recall-worker-v1", self.owner_id, family, source, seq,
                           canonical_version, self.protector.version(), self.protector.key_reference(owner_id=self.owner_id)],
                          separators=(",", ":")).encode()

    def run_once(self):
        self._gate()
        with self._connection() as db, self._transaction(db):
            event = db.execute("SELECT seq, source, kind, canonical_version, event_type, version_fenced FROM events WHERE done=0 ORDER BY seq LIMIT 1").fetchone()
            if event is None:
                return False
            seq, source, kind, canonical_version, event_type, version_fenced = event
            family = ChangeKind(kind).family
            prior = db.execute("SELECT canonical_version FROM projections WHERE family=? AND source=?", (family, source)).fetchone()
            if prior and canonical_version < prior[0]:
                db.execute("UPDATE events SET done=1 WHERE seq=?", (seq,))
                return True
            # Loader must provide complete coverage for this one source or raise.
            if version_fenced and self.load_generation is not None:
                actual_generation = self.load_generation(owner_id=self.owner_id, family=family, source_id=source)
                if actual_generation is not None and actual_generation < canonical_version:
                    raise RecallIndexError("canonical source is behind the outbox event; retrying")
                if actual_generation is not None and actual_generation > canonical_version:
                    # Canonical state already includes this event. Ack the obsolete hint;
                    # the newer outbox row will carry the current generation.
                    db.execute("UPDATE events SET done=1 WHERE seq=?", (seq,))
                    return True
            items = list(self.load_source(owner_id=self.owner_id, family=family, source_id=source))
            if len(items) > 10_000 or len({item.item_id for item in items}) != len(items):
                raise RecallIndexError("source projection incomplete or ambiguous")
            if any(item.owner_id != self.owner_id or item.source_id != source for item in items):
                raise RecallIndexError("canonical loader leaked another source or owner")
            encoded = json.dumps([_encode(item) for item in items], sort_keys=True).encode()
            if len(encoded) > MAX_SNAPSHOT_BYTES:
                raise RecallIndexError("source projection exceeds snapshot bound")
            sealed = self.protector.seal(encoded, owner_id=self.owner_id, associated_data=self._aad(family, source, seq, canonical_version))
            self._gate()
            db.execute("""INSERT INTO projections(family, source, seq, canonical_version, payload) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(family, source) DO UPDATE SET seq=excluded.seq,
                canonical_version=excluded.canonical_version, payload=excluded.payload
                WHERE excluded.canonical_version >= projections.canonical_version""",
                       (family, source, seq, canonical_version, sealed))
            db.execute("UPDATE events SET done=1 WHERE seq=?", (seq,))
            return True

    def read_items(self):
        self._gate()
        items = []
        with self._connection() as db, self._transaction(db):
            pending = {(ChangeKind(kind).family, source) for source, kind in db.execute("SELECT source, kind FROM events WHERE done=0")}
            for family, source, seq, canonical_version, sealed in db.execute("SELECT family, source, seq, canonical_version, payload FROM projections"):
                if (family, source) in pending:
                    continue  # Never expose a projection known to require refresh.
                decoded = self.protector.open(sealed, owner_id=self.owner_id, associated_data=self._aad(family, source, seq, canonical_version))
                rows = [_decode(row) for row in json.loads(decoded)]
                if any(row.owner_id != self.owner_id or row.source_id != source for row in rows):
                    raise RecallIndexError("stored projection owner/source mismatch")
                items.extend(rows)
        if len({item.item_id for item in items}) != len(items):
            raise RecallIndexError("duplicate item identities across sources")
        self._gate()
        return items
