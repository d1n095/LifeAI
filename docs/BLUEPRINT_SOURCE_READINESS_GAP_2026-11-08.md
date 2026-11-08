# Blueprint Source Readiness — Gap Analysis (2026-11-08)

> **Purpose.** Assess how the existing architecture supports turning verified historical
> knowledge into future Blueprints — a **requirements/provenance readiness** analysis, not a
> feature-building mandate. **Does not** create a parallel Blueprint engine. The existing
> Blueprint **P1 security findings remain owned by Codex's remediation lane (#360) and are
> independently reviewed by Copilot (#361)** — this document does not touch or duplicate that work.
> `first_cycle_go=false`.

## Where the Blueprint machinery lives (important)

The Blueprint engine is **not on the default branch**. It exists on the in-flight candidate
`claude/founder-alpha-blueprint-draft` (**PR #340**) and in #352's integration tree:
`backend/app/blueprint/service.py`, `backend/app/models/blueprint_draft.py`,
`backend/app/routers/blueprint.py`, migrations `0070_blueprint_drafts` / `0071_blueprint_approval_integrity`
/ `0072_blueprint_approve_identity`, tests `test_blueprint_draft.py` / `test_blueprint_approval_security.py`
/ `test_founder_alpha_blueprint_e2e.py`, and `docs/FOUNDER_ALPHA_BLUEPRINT_DRAFT.md`. All evidence
below is read from that candidate (read-only). It is unmerged and security-gated.

## Capability matrix (investigation points)

| Investigation point | Status | Evidence (candidate #340) / gap |
|---|---|---|
| **Source-grounded requirement extraction** | **EXISTS (candidate)** | `generate_blueprint_draft` **refuses an ungrounded draft** (no sources) and records `source_document_ids` + `source_refs` pointing at immutable originals; `provenance.originals_mutated == False` (`service.py:60–122`). |
| **Duplicate detection** | **PARTIAL** | Structural uniqueness only: one row per `(owner_id, title, version)` enforced by `ix_blueprint_drafts_owner_title_version`. **Semantic** duplicate-requirement detection does **not** exist. |
| **Contradiction detection** | **GAP (not wired to Blueprint)** | RAG has claim/source conflict detection (`app/rag/trust.py` `detect_conflicts` / `detect_claim_conflicts`, surfaced in chat), but it is **not** invoked during Blueprint synthesis. No contradiction gate between requirements. |
| **Historical vs. current decisions** | **PARTIAL** | Immutable per-title `version` history gives a timeline; but there is **no explicit supersede/active-vs-historical marker** on a draft (unlike `founder_memory`'s `status=superseded` / `supersedes_note_id` in `LIFE_FOUNDER_MEMORY.md`). "Current" is implicitly "highest version". |
| **Founder authority & approval** | **EXISTS (strong, candidate) — but P1-security-gated** | Approval is a separate explicit Founder action via the `SECURITY DEFINER` `blueprint_approve_draft` (migrations 0071/0072); a BEFORE UPDATE/DELETE **guard trigger makes an approved draft immutable**; Life never self-approves (`service.py:142–188`). **Caveat:** the Blueprint **source-text egress P1** is under Codex remediation (#360) + Copilot review (#361) — approval readiness is blocked on that gate, not on anything in this analysis. |
| **Requirement dependencies** | **GAP** | No dependency edges between requirements or drafts; a Blueprint is a single `body` + flat `source_refs`. No DAG / prerequisite modeling. |
| **Architecture decision records (ADR)** | **GAP** | No ADR entity or structure. Decisions live implicitly in docs and `founder_memory` notes, not as first-class, linkable ADRs a Blueprint could cite. |
| **Blueprint versioning** | **EXISTS (candidate)** | Immutable, concurrency-safe version allocation (`max(version)+1` guarded by the unique index, bounded retry on 23505; `service.py:98–138`). |
| **Auditability & rollback** | **PARTIAL** | Strong audit trail (immutable versions + approval guard + append-only `AuditLog`). But "rollback" is **forward-only** — you regenerate a new version; there is no "re-activate an earlier approved version" concept, and no cross-version active pointer. |

## Readiness verdict

**Ready (on the candidate, once its security gate clears):** source-grounded generation that refuses ungrounded drafts, immutable provenance to originals, Founder-only DB-enforced approval with an immutability guard, and immutable versioning. These are the load-bearing *provenance* properties this mission asked about, and they already exist.

**Genuine gaps before historical knowledge can become comprehensive Blueprints:**
1. **Contradiction detection at synthesis time** — reuse the existing `trust.detect_conflicts` / `detect_claim_conflicts` rather than build anew; wire it into `generate_blueprint_draft` so a draft grounded in mutually-contradictory sources is flagged, not silently synthesized.
2. **Semantic duplicate-requirement detection** — beyond exact-checksum ingestion dedup.
3. **Requirement dependencies** — a prerequisite/ordering model so a Blueprint can express "B depends on A".
4. **ADR as a first-class, citable entity** — so historical decisions (and their supersession) are linkable from Blueprints, reusing `founder_memory`'s existing supersede semantics rather than re-inventing them.
5. **Active-version pointer / revertible approval** — an explicit "current approved" marker distinct from "highest version", enabling an auditable revert.

## Explicit non-goals / ownership

- **No parallel Blueprint engine** is proposed or built; all gaps above are framed as *extensions that reuse existing primitives* (`trust.*` conflict detection, `founder_memory` supersede semantics, the existing versioning/approval machinery).
- The **Blueprint source-text egress P1** and its remediation are **owned by Codex (#360) and independently reviewed by Copilot (#361)** — out of scope here and untouched.
- Nothing here is implemented; this is a readiness/provenance map for Founder review. `first_cycle_go=false`.

*Builder (Claude Code). Documentation-only; review-pending.*
