"""Embedding-space identity, zero-pad fitting, and the no-silent-mixing retrieval guard
(app/rag/embedding_space.py + app/rag/vector_store.py). Unit tests prove the cosine-preserving
pad math and fail-closed behavior; the DB tests run against real local Postgres with RLS, the
same way the rest of tests/backend/rag/ does, and prove that retrieval scoped to an active
(model, native_dim) never returns chunks from a different space, that cross-owner isolation
still holds, and that legacy NULL-identity rows are invisible to a scoped query but still
reachable unscoped (backward compatibility)."""

import math
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.db import migration_engine
from app.models.document import Document, IndexStatus
from app.rag.embedding_space import (
    EmbeddingSpaceError,
    fit_vectors,
    native_dim_of,
    pad_to_storage,
)
from app.rag.vector_store import search, upsert_chunks

STORAGE = get_settings().embedding_dim


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb)


# --------------------------- unit: pad / fit math ---------------------------

def test_zero_padding_preserves_cosine_exactly():
    a = [0.3, -0.7, 0.1, 0.9, -0.2, 0.5]
    b = [-0.1, 0.4, 0.8, -0.3, 0.6, 0.2]
    native_cos = _cosine(a, b)
    padded_cos = _cosine(pad_to_storage(a, STORAGE), pad_to_storage(b, STORAGE))
    assert padded_cos == pytest.approx(native_cos, abs=1e-12)


def test_pad_passthrough_at_storage_width():
    v = [0.1] * STORAGE
    out = pad_to_storage(v, STORAGE)
    assert len(out) == STORAGE
    assert out == v
    assert out is not v  # a copy, never the caller's list


def test_pad_lengthens_to_storage_width():
    out = pad_to_storage([1.0, 2.0, 3.0], STORAGE)
    assert len(out) == STORAGE
    assert out[:3] == [1.0, 2.0, 3.0]
    assert set(out[3:]) == {0.0}


def test_pad_fails_closed_when_native_exceeds_storage():
    with pytest.raises(EmbeddingSpaceError):
        pad_to_storage([0.0] * (STORAGE + 1), STORAGE)


def test_fit_vectors_reports_native_dim_and_pads():
    vectors = [[0.2] * 768, [0.3] * 768]
    padded, native = fit_vectors(vectors, STORAGE)
    assert native == 768
    assert all(len(v) == STORAGE for v in padded)


def test_fit_vectors_rejects_mixed_dimension_batch():
    with pytest.raises(EmbeddingSpaceError):
        fit_vectors([[0.1] * 768, [0.1] * 1024], STORAGE)


def test_native_dim_of_empty_batch_is_zero():
    assert native_dim_of([]) == 0


# --------------------------- DB: no-mixing + isolation ---------------------------

def _set_rls_user(db_session, owner_id) -> None:
    from app.request_context import current_user_id as current_user_id_var

    current_user_id_var.set(str(owner_id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_doc(db_session, owner_id, title) -> Document:
    doc = Document(
        uploaded_by=owner_id,
        title=title,
        original_filename=title,
        media_type="text/plain",
        checksum=uuid.uuid4().hex * 2,  # 64 hex chars
        status=IndexStatus.indexed,
    )
    db_session.add(doc)
    db_session.flush()
    return doc


def test_search_is_scoped_to_active_space_and_never_mixes_models(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)

    # Two sources, two different embedding spaces. Give model-b's chunk a vector IDENTICAL to
    # the query so that, if the active-space filter were absent, cosine ranking would surface
    # it — making the filter (not mere ranking) the thing under test.
    doc_a = _make_doc(db_session, user.id, "source-a.txt")
    doc_b = _make_doc(db_session, user.id, "source-b.txt")

    query_native = [1.0] + [0.0] * 767  # native dim 768
    a_vec = [0.9] + [0.1] * 767         # model-a, native 768, near the query
    b_vec = [1.0] + [0.0] * 1023        # model-b, native 1024, identical prefix to the query

    padded_a, native_a = fit_vectors([a_vec], STORAGE)
    padded_b, native_b = fit_vectors([b_vec], STORAGE)
    upsert_chunks(db_session, doc_a.id, user.id, ["chunk from model a"], padded_a,
                  embedding_model="model-a", embedding_native_dim=native_a)
    upsert_chunks(db_session, doc_b.id, user.id, ["chunk from model b"], padded_b,
                  embedding_model="model-b", embedding_native_dim=native_b)
    db_session.commit()

    # Query in model-a's space: only model-a's chunk is comparable, even though model-b's
    # vector is a closer cosine match — no silent cross-model mixing.
    hits_a = search(db_session, user.id, query_native, top_k=10,
                    embedding_model="model-a", embedding_native_dim=768)
    assert [h["text"] for h in hits_a] == ["chunk from model a"]

    # Query in model-b's space: only model-b's chunk.
    hits_b = search(db_session, user.id, query_native, top_k=10,
                    embedding_model="model-b", embedding_native_dim=1024)
    assert [h["text"] for h in hits_b] == ["chunk from model b"]

    # A space that stamped nothing returns nothing (not the nearest foreign-space vector).
    hits_none = search(db_session, user.id, query_native, top_k=10,
                       embedding_model="model-c", embedding_native_dim=384)
    assert hits_none == []


def test_active_space_search_keeps_cross_owner_isolation(db_session, superuser_db, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    doc_a = _make_doc(db_session, user_a.id, "owner-a-secret.txt")
    padded, native = fit_vectors([[1.0] + [0.0] * 767], STORAGE)
    upsert_chunks(db_session, doc_a.id, user_a.id, ["owner A private chunk"], padded,
                  embedding_model="model-a", embedding_native_dim=native)
    db_session.commit()

    # Owner B, same active space, same query vector — must see nothing of owner A's.
    _set_rls_user(db_session, user_b.id)
    hits = search(db_session, user_b.id, [1.0] + [0.0] * 767, top_k=10,
                  embedding_model="model-a", embedding_native_dim=768)
    assert hits == []


def test_legacy_null_identity_chunk_invisible_to_scoped_query_but_reachable_unscoped(
    db_session, make_verified_user
):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    doc = _make_doc(db_session, user.id, "legacy.txt")
    # A pre-migration row: no embedding_model/native_dim stamped, stored at full storage width.
    upsert_chunks(db_session, doc.id, user.id, ["legacy unstamped chunk"],
                  [pad_to_storage([0.5] + [0.0] * 767, STORAGE)])
    db_session.commit()

    query = [0.5] + [0.0] * 767
    # A scoped query (a real active model) never returns the NULL-identity legacy row.
    scoped = search(db_session, user.id, query, top_k=10,
                    embedding_model="model-a", embedding_native_dim=768)
    assert scoped == []
    # Unscoped (no active space passed) still finds it — backward compatibility for any caller
    # that has not adopted the identity arguments.
    unscoped = search(db_session, user.id, query, top_k=10)
    assert [h["text"] for h in unscoped] == ["legacy unstamped chunk"]


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"embedding_model": "model-a"},
        {"embedding_native_dim": 768},
    ],
)
def test_retrieval_refuses_missing_or_partial_embedding_space_identity(
    db_session, make_verified_user, identity
):
    """No public semantic-search call may silently fall back to cross-space ranking."""
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    doc_a = _make_doc(db_session, user.id, "space-a.txt")
    doc_b = _make_doc(db_session, user.id, "space-b.txt")
    padded_a, native_a = fit_vectors([[1.0] + [0.0] * 767], STORAGE)
    padded_b, native_b = fit_vectors([[1.0] + [0.0] * 1023], STORAGE)
    upsert_chunks(
        db_session,
        doc_a.id,
        user.id,
        ["model-a chunk"],
        padded_a,
        embedding_model="model-a",
        embedding_native_dim=native_a,
    )
    upsert_chunks(
        db_session,
        doc_b.id,
        user.id,
        ["model-b chunk"],
        padded_b,
        embedding_model="model-b",
        embedding_native_dim=native_b,
    )
    db_session.commit()

    with pytest.raises(EmbeddingSpaceError):
        search(
            db_session,
            user.id,
            [1.0] + [0.0] * 767,
            top_k=10,
            **identity,
        )


def test_query_dimension_must_match_declared_embedding_space(db_session, make_verified_user):
    """A caller must not relabel a 768-dimensional query as belonging to a 1024-d space."""
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    doc = _make_doc(db_session, user.id, "space-1024.txt")
    padded, native = fit_vectors([[1.0] + [0.0] * 1023], STORAGE)
    upsert_chunks(
        db_session,
        doc.id,
        user.id,
        ["1024-dimensional chunk"],
        padded,
        embedding_model="model-b",
        embedding_native_dim=native,
    )
    db_session.commit()

    with pytest.raises(EmbeddingSpaceError):
        search(
            db_session,
            user.id,
            [1.0] + [0.0] * 767,
            top_k=10,
            embedding_model="model-b",
            embedding_native_dim=1024,
        )


def test_0069_downgrade_upgrade_preserves_embedding_space_identity(superuser_db, make_verified_user):
    """A claimed data-safe rollback must not erase the provenance needed for safe retrieval."""
    user, _ = make_verified_user()
    doc = _make_doc(superuser_db, user.id, "migration-provenance.txt")
    padded, native = fit_vectors([[1.0] + [0.0] * 767], STORAGE)
    upsert_chunks(
        superuser_db,
        doc.id,
        user.id,
        ["identity must survive rollback"],
        padded,
        embedding_model="model-a",
        embedding_native_dim=native,
    )
    superuser_db.commit()
    chunk_id = superuser_db.execute(
        sa_text("SELECT id FROM document_chunks WHERE document_id = :doc_id"),
        {"doc_id": str(doc.id)},
    ).scalar_one()
    superuser_db.close()
    migration_engine.dispose()

    backend_root = Path(__file__).resolve().parents[3]
    env = {**os.environ}
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "0068"],
            cwd=backend_root,
            env=env,
            check=True,
        )
    finally:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "0069_embedding_space_identity"],
            cwd=backend_root,
            env=env,
            check=True,
        )

    migration_engine.dispose()
    with migration_engine.connect() as conn:
        row = conn.execute(
            sa_text(
                "SELECT embedding_model, embedding_native_dim "
                "FROM document_chunks WHERE id = :chunk_id"
            ),
            {"chunk_id": str(chunk_id)},
        ).one()
    assert row == ("model-a", 768)
