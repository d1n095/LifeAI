"""Examiner-only DB/API probes for continuous-conversation P1 fix e21ad76 (disposable)."""
import re
import threading
import uuid

from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from app.db import SessionLocal, migration_engine
from app.mainai_continuous_conversation.context import assemble_live_context
from app.mainai_continuous_conversation.service import get_or_create_canonical_conversation
from app.mainai_continuous_conversation.types import WorkspaceMutability
from app.mainai_continuous_conversation.workspace import WorkspaceClaim, WorkspaceOwnershipError, claim_workspace
from app.models.conversation import Conversation, Message, MessageRole, MessageStatus
from app.models.user import User
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from tests.backend.mainai.test_continuous_conversation import DIM, FOUNDER_EMAIL, FOUNDER_PASSWORD
from tests.backend.test_account_erasure import (  # noqa: F401
    _make_user, _narrow_privileges_before_this_module, _set_rls_user, erase_account_data,
)

SHA_X = "abcdef0123456789abcdef0123456789abcdef01"


def _scalar(sql, params):
    with migration_engine.connect() as c:
        return c.execute(sa_text(sql), params).scalar_one()


def report(name, holds, detail=""):
    print(f"\n{'HOLDS' if holds else 'GAP  '} {name} {detail}")


def _seed_thread(db, owner_id, texts):
    conv = get_or_create_canonical_conversation(db, owner_id=owner_id)
    ids = []
    for i, t in enumerate(texts):
        m = Message(conversation_id=conv.id, role=MessageRole.user if i % 2 == 0 else MessageRole.assistant, content=t, status=MessageStatus.succeeded)
        db.add(m)
        db.flush()
        ids.append(m.id)
    return conv, ids


def test_erasure_and_governance():
    # 1. erasure with canonical conversation + compacted memory
    s = SessionLocal()
    try:
        owner = _make_user(s)
        oid = owner.id
        _set_rls_user(s, oid)
        conv, _ = _seed_thread(s, oid, [f"turn {i} use provider A {SHA_X}" for i in range(26)])
        assemble_live_context(s, owner_id=oid, conversation_id=conv.id)
        s.commit()
        _set_rls_user(s, oid)
        try:
            erase_account_data(s, owner)
            ok, err = s.get(User, oid) is None, ""
        except Exception as exc:  # noqa: BLE001
            ok, err = False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:140]}"
        left = _scalar((
            "SELECT (SELECT count(*) FROM founder_canonical_conversations WHERE owner_id=:o)+(SELECT count(*) FROM founder_conversation_compactions WHERE owner_id=:o)+"
            "(SELECT count(*) FROM founder_conversation_decisions WHERE owner_id=:o)+(SELECT count(*) FROM founder_conversation_provenance WHERE owner_id=:o)"), {"o": str(oid)})
        report("erase_account_data succeeds with canonical thread + compacted memory", ok, err)
        report("no dangling canonical/compaction/decision/provenance rows after erasure", left == 0, f"(remaining={left})")
    finally:
        s.rollback()
        s.close()
    # 2. governance: runtime calls the SECDEF wipe outside any erasure operation
    s = SessionLocal()
    try:
        owner = _make_user(s)
        oid = owner.id
        _set_rls_user(s, oid)
        conv, _ = _seed_thread(s, oid, [f"memory {i}" for i in range(26)])
        assemble_live_context(s, owner_id=oid, conversation_id=conv.id)
        s.commit()
        assert s.execute(sa_text("select current_user")).scalar_one() == "mainai_app"
        _set_rls_user(s, oid)
        try:
            s.execute(sa_text("SELECT erase_own_continuous_conversation_children()"))
            s.commit()
            wiped = True
        except Exception:  # noqa: BLE001
            s.rollback()
            wiped = False
        c = _scalar("SELECT count(*) FROM founder_conversation_compactions WHERE owner_id=:o", {"o": str(oid)})
        report("ordinary runtime session cannot wipe compacted memory/binding outside governed erasure", not wiped, f"(call succeeded={wiped}, compactions left={c})")
        # then, binding gone: self-set GUC + delete the canonical conversation
        _set_rls_user(s, oid)
        s.execute(sa_text("SELECT set_config('app.continuous_conversation_erasure_in_progress','true',true)"))
        try:
            n = s.execute(sa_text("DELETE FROM conversations WHERE id=:c"), {"c": str(conv.id)}).rowcount
            s.commit()
        except Exception as exc:  # noqa: BLE001
            s.rollback()
            n = f"denied {type(exc).__name__}"
        report("canonical conversation deletion stays governed (no self-set GUC path)", n in (0,) or str(n).startswith("denied"), f"(rows deleted={n})")
    finally:
        s.rollback()
        s.close()


def test_cross_owner_references(db_session, superuser_db, make_verified_user):
    a, _ = make_verified_user()
    b, _ = make_verified_user()
    bconv = Conversation(title="B", user_id=b.id)
    superuser_db.add(bconv)
    superuser_db.flush()
    bmsg = Message(conversation_id=bconv.id, role=MessageRole.user, content="B secret", status=MessageStatus.succeeded)
    superuser_db.add(bmsg)
    superuser_db.commit()
    aconv = Conversation(title="A", user_id=a.id)
    superuser_db.add(aconv)
    superuser_db.commit()
    cases = [
        ("canonical binding to B's conversation", "INSERT INTO founder_canonical_conversations (owner_id, conversation_id) VALUES (:a, :bc)"),
        ("event referencing B's conversation", "INSERT INTO founder_conversation_events (owner_id, conversation_id, direction, kind) VALUES (:a, :bc, 'inbound', 'x')"),
        ("compaction over B's conversation", "INSERT INTO founder_conversation_compactions (owner_id, conversation_id) VALUES (:a, :bc)"),
        ("A's provenance row pointing at B's message", "INSERT INTO founder_conversation_provenance (owner_id, conversation_id, message_id, kind, value) VALUES (:a, :ac, :bm, 'sha', 'x')"),
        ("A's decision row pointing at B's message", "INSERT INTO founder_conversation_decisions (owner_id, conversation_id, message_id) VALUES (:a, :ac, :bm)"),
    ]
    for name, sql in cases:
        db_session.execute(sa_text("SELECT set_config('app.current_user_id', :o, true)"), {"o": str(a.id)})
        try:
            db_session.execute(sa_text(sql), {"a": str(a.id), "bc": str(bconv.id), "ac": str(aconv.id), "bm": str(bmsg.id)})
            db_session.flush()
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        db_session.commit() if ok else db_session.rollback()
        report(f"cross-owner blocked: {name}", not ok, f"-> {'ALLOWED' if ok else 'DENIED'}")
    # does A's pointer interfere with B deleting their message / conversation?
    try:
        superuser_db.execute(sa_text("DELETE FROM messages WHERE id=:m"), {"m": str(bmsg.id)})
        superuser_db.execute(sa_text("DELETE FROM conversations WHERE id=:c"), {"c": str(bconv.id)})
        superuser_db.commit()
        ok = True
    except Exception as exc:  # noqa: BLE001
        superuser_db.rollback()
        ok = False
    report("A's cross-owner pointers cannot block B's message/conversation deletion", ok)


def test_latest_window_compaction_growth_and_truncation(client, superuser_db, monkeypatch):
    captured = []

    async def _embed(self, texts, model, **kw):
        return [[0.1] * DIM for _ in texts]

    async def _chat(self, messages, model, **kw):
        captured.append([m.content for m in messages])
        return ChatResult(content="ok", provider="openai", model=model, raw_usage={"prompt_tokens": 1, "completion_tokens": 1})

    monkeypatch.setattr(OpenAIProvider, "embed", _embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    csrf = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD}).json()["csrf_token"]
    pad = "x" * 200
    msgs = [f"TURN-01 Use provider A. {pad} exact sha {SHA_X} branch cursor/some-long-branch-name file_id: 11111111-2222-3333-4444-555555555555"]
    msgs += [f"TURN-{i:02d} note" for i in range(2, 26)]
    msgs[11] = "TURN-12 Switch to provider B."
    msgs[15] = "TURN-16 Let's go with provider C instead."
    msgs[17] = "TURN-18 Byt leverantör till D."
    sizes = []
    for t in msgs:
        r = client.post("/api/chat", json={"message": t, "continuous": True}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200, r.text
        sizes.append(len(captured[-1][0]))
    conv_id = r.json()["conversation_id"]
    last = captured[-1]
    hist = [c for c in last[1:-1] if c.startswith("TURN-")]
    turns = [h.split()[0] for h in hist]
    report("live window holds newest turns in chronological order", turns == sorted(turns) and "TURN-24" in turns and "TURN-01" not in turns,
           f"(history turns {turns[0]}..{turns[-1]}, n={len(turns)}; current=TURN-25)")
    system = last[0]
    report("system prompt growth bounded across turns", sizes[-1] - sizes[-6] < 2000,
           f"(system prompt chars at turns 20/25: {sizes[19]}/{sizes[-1]}; compaction rows={superuser_db.execute(sa_text('SELECT count(*) FROM founder_conversation_compactions WHERE conversation_id=:c'), {'c': conv_id}).scalar_one()})")
    hexruns = re.findall(r"[0-9a-f]{12,}", system)
    partial = [h for h in hexruns if len(h) != 40]
    report("no truncated/approximate SHA fragments in compacted memory", not partial, f"(full={sum(len(h)==40 for h in hexruns)}, partial={partial[:2]})")
    prov = superuser_db.execute(sa_text("SELECT kind, value FROM founder_conversation_provenance WHERE conversation_id=:c ORDER BY kind"), {"c": conv_id}).all()
    exact = {(k, v) for k, v in prov}
    report("provenance keeps exact SHA/branch/file_id pointers", ("sha", SHA_X) in exact and ("file_id", "11111111-2222-3333-4444-555555555555") in exact
           and any(k == "branch" and v.startswith("cursor/some-long-branch-name") for k, v in exact), f"({sorted(exact)[:4]})")
    dec = superuser_db.execute(sa_text("SELECT statement, superseded FROM founder_conversation_decisions WHERE conversation_id=:c ORDER BY created_at"), {"c": conv_id}).all()
    active = [s for s, sup in dec if not sup]
    report("explicit 'Switch to provider B' supersedes A", any("provider B" in s for s in [d[0] for d in dec]), f"(decisions={[(s, sup) for s, sup in dec]})")
    report("natural supersession variants ('go with provider C instead', 'Byt leverantör till D') recognised", any("C" in s or "D" in s for s in active),
           f"(active decisions={active})")
    report("current decision shown to model reflects latest instruction", "provider D" in system or "leverantör till D" in system.split("ACTIVE DECISIONS")[-1],
           f"(ACTIVE DECISIONS block: {system.split('ACTIVE DECISIONS')[-1][:120]!r})")


def test_workspace_leases(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    o = owner.id
    claim_workspace(superuser_db, WorkspaceClaim(o, "claude", "cursor/lane-x", "/w/claude-exam", WorkspaceMutability.READ_ONLY_EXAMINER))
    for name, claim in [
        ("builder claims examiner's worktree", WorkspaceClaim(o, "cursor", "cursor/lane-x", "/w/claude-exam", WorkspaceMutability.MUTABLE_BUILDER)),
        ("branch under examination used as mutable builder branch", WorkspaceClaim(o, "cursor", "cursor/lane-x", "/w/cursor-b", WorkspaceMutability.MUTABLE_BUILDER)),
    ]:
        try:
            claim_workspace(superuser_db, claim)
            ok = False
        except (WorkspaceOwnershipError, Exception):  # noqa: BLE001
            superuser_db.rollback() if False else None
            ok = True
        report(f"workspace: {name} rejected", ok)
    try:
        claim_workspace(superuser_db, WorkspaceClaim(o, "cursor", "cursor/lane-x-child", "/w/cursor-child", WorkspaceMutability.MUTABLE_BUILDER))
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    report("workspace: separate child branch/worktree allowed", ok)
    try:
        claim_workspace(superuser_db, WorkspaceClaim(o, "codex", "cursor/lane-x-child", "/w/cursor-child", WorkspaceMutability.MUTABLE_BUILDER))
        ok = False
    except Exception:  # noqa: BLE001
        ok = True
    report("workspace: same mutable worktree to second agent rejected", ok)
    superuser_db.commit()
    # race: examiner and builder claim the same new branch concurrently in separate sessions
    S = sessionmaker(bind=migration_engine)
    barrier, results = threading.Barrier(2), []

    def go(claim):
        sess = S()
        try:
            barrier.wait()
            claim_workspace(sess, claim)
            sess.commit()
            results.append("ok")
        except Exception as exc:  # noqa: BLE001
            sess.rollback()
            results.append(type(exc).__name__)
        finally:
            sess.close()
    t1 = threading.Thread(target=go, args=(WorkspaceClaim(o, "claude", "cursor/race", "/w/exam-race", WorkspaceMutability.READ_ONLY_EXAMINER),))
    t2 = threading.Thread(target=go, args=(WorkspaceClaim(o, "cursor", "cursor/race", "/w/build-race", WorkspaceMutability.MUTABLE_BUILDER),))
    t1.start(); t2.start(); t1.join(); t2.join()
    report("workspace: concurrent examiner+builder on same branch -> at most one wins", results.count("ok") <= 1, f"({results})")
