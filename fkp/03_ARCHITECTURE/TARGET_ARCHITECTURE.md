# Target Architecture
**Sources:** `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md`, `07_MULTI_AGENT_ORCHESTRATION_AND_MODES.md`, `04_MEMORY_CONTEXT_AND_MEANING_ENGINE.md` (all from MainAI_Conversation_Knowledge_Pack_2026-07-19), design review sessions (prior conversation).
**Carried forward from FKP v1.0 with one added caveat** (see boxed note below) — the platform-layers vision and Conversation Context Resolver summary are stack-agnostic and not affected by the v1 → v1.1 correction. The MainAI data model section specifically needs the caveat.
**Status:** DESIGNED. None of this is built yet except what is noted in `03_ARCHITECTURE/BUILT_VS_DESIGNED.md`.

---

> **v1.1 caveat on the MainAI data model section below:** UPGRADE_26 v2 was designed against a Supabase/Postgres-RLS stack (`auth.uid()`-based policies) for the unrelated `savings-story-scanner` codebase. LifeAI is FastAPI/SQLAlchemy/Alembic with its own isolation mechanism (`backend/app/rls.py`). Do not apply this SQL directly. It must be compared table-for-table and capability-for-capability against LifeAI's actual current model (see `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md`) before any migration is written. See `07_CONFLICTS_AND_GAPS/CONFLICT_REGISTER.md` CONFLICT-04 and `02_DECISIONS/DECISION_REGISTER.md` D-17.

## Platform layers

```
Founder
  │ vision / approval / irreversible decisions
  ▼
MainAI / FounderAI  ←── Founder Vault
  │ orchestrates agents, maintains project graph, controls trust
  ├── Founder UserAI (separate identity, no implicit root access)
  ├── Specialist agents (Claude Code, ChatGPT, Gemini, Kimi, Lovable, local)
  └── Source Hub (documents, chats, code, media import)
        │
        ▼
Knowledge Layer
  ├── raw_material (untrusted, append-only)
  ├── knowledge_objects (human-approved, epistemic model)
  ├── knowledge_object_sources (provenance)
  └── knowledge_promotion_events (append-only approval trail)
        │
        ▼
Life OS Modules (existing + future)
  finance, salary, calendar, documents, health, food, travel, goals,
  smart home, social, investigations, world graph
        │
        ▼
Future Users / Life City
  (isolated UserAI per user, public onboarding via Life City — DEFERRED)
```

## MainAI data model (designed, UPGRADE_26 v2 — see v1.1 caveat above)
10 new tables: `ai_projects`, `raw_material`, `knowledge_objects`, `knowledge_object_sources`, `knowledge_promotion_events`, `ai_audit_events`, `ai_conversations`, `ai_messages`, `ai_documents`, `ai_usage_records`.
18 SECURITY DEFINER functions for all state transitions.
Full details in the FKP raw-originals archive: `01_sql_migration/UPGRADE_26_v2__main_ai_foundation.sql`.

Note: LifeAI already has working, simpler precursors to several of these tables' *purpose* (not their exact shape) — `backend/app/models/conversation.py`, `backend/app/models/document.py`/`document_chunk.py`, `backend/app/models/usage.py` — see `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md`. Any adaptation of UPGRADE_26 v2 should start from what already exists, not overwrite it.

## Conversation Context Resolver (binding standard)
Priority order for every message:
1. Current message and explicit command
2. Explicit corrections in current turn
3. Last 3–5 messages verbatim with roles and timestamps
4. Last 15 messages as structured timeline
5. Active task / current page / current project phase / pending action
6. Applicable confirmed decisions
7. Relevant durable memory (by identity and scope)
8. Supporting documents, code, tests
9. Older summaries only if not contradicted

## AI Resource Orchestration (designed)
See `AI_RESOURCE_ORCHESTRATION.md` and `ADAPTIVE_WORK_ORCHESTRATION.md` in this package.

## GitHub integration (future)
Not yet built. Architecture prepared via RepositoryProvider interface. Will require a GitHub App (not PAT) with limited scopes: read code, create branches, propose changes, create PRs, read CI, request merge. Must never write directly to main without founder approval.

## Update Center (future — design candidate, not decided architecture)
UI on Life OS homepage showing prepared PRs, test results, diffs, and a one-button deploy flow. Classified as design candidate per `01_FOUNDER_VISION/DOMAIN_AND_REQUIREMENT_MAP.md` and `02_DECISIONS/CANDIDATE_REQUIREMENTS.md` §9.
