-- S1A production data profile — read-only, no writes.
--
-- Purpose: profile how many `knowledge_claims` rows are still S1A backfill candidates
-- (memory_source_id IS NULL) and how they'd resolve under
-- backend/app/rag/memory_source_backfill.py::_resolve_locator(), BEFORE any real backfill
-- run. See docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8.
--
-- This is operator documentation, not application code or a migration. It is never imported,
-- executed automatically, or referenced by the app/worker/CI. Run it manually, by hand, by
-- someone with real production database access.
--
-- Usage (operator, from a host with real DATABASE_URL / psql access):
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f docs/operations/s1a_production_profile.sql
-- The whole file is wrapped in an explicit READ ONLY transaction that always ends in
-- ROLLBACK — even a typo'd stray statement pasted into the same session cannot write.
--
-- Mirrors _resolve_locator() EXACTLY:
--   1. document = get(Document, claim.source_id); must exist and document.uploaded_by = claim.owner_id
--   2. if chunk_id is not NULL: chunk must exist, chunk.document_id = claim.source_id, chunk.owner_id = claim.owner_id
--      -> resolves "exact" (chunk-backed)
--   3. elif version_id is not NULL: version must exist, version.source_id = claim.source_id, version.owner_id = claim.owner_id
--      -> resolves "degraded" (version-backed)
--   4. else -> resolves "missing" (document-backed only)
--   Anything that fails its structural check at steps 1-3 is unresolvable ("fail-closed").
--   No deleted_at filter anywhere, matching _resolve_locator() precisely (not an independent guess).
--
-- Scope: only knowledge_claims rows with memory_source_id IS NULL are backfill candidates
-- (rows that already have memory_source_id set are already migrated / out of scope).

BEGIN TRANSACTION READ ONLY;

-- ============================================================
-- 1. Overall scale counts
-- ============================================================
SELECT
    (SELECT count(*) FROM documents)          AS total_documents,
    (SELECT count(*) FROM document_chunks)    AS total_document_chunks,
    (SELECT count(*) FROM knowledge_versions) AS total_knowledge_versions,
    (SELECT count(*) FROM knowledge_claims)   AS total_knowledge_claims,
    (SELECT count(*) FROM knowledge_claims WHERE memory_source_id IS NOT NULL) AS claims_already_migrated,
    (SELECT count(*) FROM knowledge_claims WHERE memory_source_id IS NULL)     AS claims_backfill_candidates;

-- ============================================================
-- 2. Full 2x2 null-combination breakdown of (chunk_id, version_id)
--    among backfill candidates (memory_source_id IS NULL)
--    (extends the original 3-cell design-doc query with the 4th "both set" cell)
-- ============================================================
SELECT
    count(*) FILTER (WHERE chunk_id IS NOT NULL AND version_id IS NOT NULL) AS chunk_and_version_both_set,
    count(*) FILTER (WHERE chunk_id IS NOT NULL AND version_id IS NULL)     AS chunk_only,
    count(*) FILTER (WHERE chunk_id IS NULL     AND version_id IS NOT NULL) AS version_only,
    count(*) FILTER (WHERE chunk_id IS NULL     AND version_id IS NULL)     AS neither
FROM knowledge_claims
WHERE memory_source_id IS NULL;

-- ============================================================
-- 3. Resolution-tier classification, mirroring _resolve_locator() exactly
-- ============================================================
WITH candidates AS (
    SELECT kc.id, kc.owner_id, kc.source_id, kc.chunk_id, kc.version_id
    FROM knowledge_claims kc
    WHERE kc.memory_source_id IS NULL
),
resolved AS (
    SELECT
        c.id,
        c.owner_id,
        c.source_id,
        c.chunk_id,
        c.version_id,
        d.id IS NOT NULL AS document_ok,
        CASE
            WHEN d.id IS NULL THEN 'unresolvable_document_missing_or_not_owned'
            WHEN c.chunk_id IS NOT NULL THEN
                CASE
                    WHEN dc.id IS NOT NULL AND dc.document_id = c.source_id AND dc.owner_id = c.owner_id
                        THEN 'exact_chunk'
                    ELSE 'unresolvable_chunk_id_structurally_invalid'
                END
            WHEN c.version_id IS NOT NULL THEN
                CASE
                    WHEN kv.id IS NOT NULL AND kv.source_id = c.source_id AND kv.owner_id = c.owner_id
                        THEN 'degraded_version'
                    ELSE 'unresolvable_version_id_structurally_invalid'
                END
            ELSE 'missing_document_only'
        END AS resolution_tier
    FROM candidates c
    LEFT JOIN documents d
        ON d.id = c.source_id AND d.uploaded_by = c.owner_id
    LEFT JOIN document_chunks dc
        ON dc.id = c.chunk_id
    LEFT JOIN knowledge_versions kv
        ON kv.id = c.version_id
)
SELECT resolution_tier, count(*)
FROM resolved
GROUP BY resolution_tier
ORDER BY count(*) DESC;

-- ============================================================
-- 4. Detailed listing of unresolvable rows (fail-closed candidates), with reason.
--    Identifiers only (claim/owner/source/chunk/version ids) — no claim_text or other
--    content. Capped at 200; raise/remove the LIMIT for the full set if needed.
-- ============================================================
WITH candidates AS (
    SELECT kc.id, kc.owner_id, kc.source_id, kc.chunk_id, kc.version_id
    FROM knowledge_claims kc
    WHERE kc.memory_source_id IS NULL
)
SELECT
    c.id AS claim_id,
    c.owner_id,
    c.source_id,
    c.chunk_id,
    c.version_id,
    CASE
        WHEN d.id IS NULL THEN 'document ' || c.source_id || ' not found or not owned by owner ' || c.owner_id
        WHEN c.chunk_id IS NOT NULL AND NOT (dc.id IS NOT NULL AND dc.document_id = c.source_id AND dc.owner_id = c.owner_id)
            THEN 'chunk_id ' || c.chunk_id || ' does not structurally belong to document ' || c.source_id || '/owner ' || c.owner_id
        WHEN c.version_id IS NOT NULL AND NOT (kv.id IS NOT NULL AND kv.source_id = c.source_id AND kv.owner_id = c.owner_id)
            THEN 'version_id ' || c.version_id || ' does not structurally belong to document ' || c.source_id || '/owner ' || c.owner_id
        ELSE NULL
    END AS failure_reason
FROM candidates c
LEFT JOIN documents d
    ON d.id = c.source_id AND d.uploaded_by = c.owner_id
LEFT JOIN document_chunks dc
    ON dc.id = c.chunk_id
LEFT JOIN knowledge_versions kv
    ON kv.id = c.version_id
WHERE
    d.id IS NULL
    OR (c.chunk_id IS NOT NULL AND NOT (dc.id IS NOT NULL AND dc.document_id = c.source_id AND dc.owner_id = c.owner_id))
    OR (c.version_id IS NOT NULL AND NOT (kv.id IS NOT NULL AND kv.source_id = c.source_id AND kv.owner_id = c.owner_id))
ORDER BY c.id
LIMIT 200;

ROLLBACK;
