# Rejected Alternatives
**Source:** `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` (MainAI_Conversation_Knowledge_Pack_2026-07-19), plus one addition verified in this FKP v1.1 pass.
**Note:** Only alternatives explicitly rejected in source material, or rejected with documented reasoning during implementation. Nothing inferred.

---

| Area | Rejected option | Reason given in source | Source |
|------|----------------|----------------------|--------|
| Deployment | Render Blueprint multi-service topology | Cost exceeds 0 SEK/month target | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` |
| Deployment | Vercel | Set aside (reason not elaborated in source) | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` |
| Auth | Public registration for Step 1 | MainAI is founder-only; public users not in scope for Step 1 | `01_EXECUTIVE_CONTEXT_AND_CURRENT_STATE.md` — **implemented**, see D-27 |
| Data model | Shared `documents`, `projects`, `tasks` tables for all users | Prevents UserAI isolation; requires ownership/RLS migration | `01_EXECUTIVE_CONTEXT_AND_CURRENT_STATE.md` — still pending, see OD-04 |
| AI memory | One enormous prompt as project memory | Loses detail; structured project intelligence preferred | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` |
| AI trust | Trusting AI self-assertion of capability | Must be behaviorally verified with blind/adversarial tests | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` |
| Decisions table | Standalone `ai_decisions` table | Retired; decisions are `knowledge_objects.object_type='decision'` | Design review (prior conversation) |
| Knowledge model | Single `memory_type` enum combining object type and epistemic status | Conflates orthogonal dimensions; replaced by 4 independent status fields | Design review (prior conversation) |
| Hash function | `extensions.digest()` for SHA-256 in migration | Assumes pgcrypto in `extensions` schema; replaced by `pg_catalog.sha256()` | Local PG16 test run (prior conversation) |
| Agent orchestration | One AI carrying the whole project alone | Inefficient; work routed across specialized agents with handovers | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` |
| Auth scope (new in v1.1) | Gating *all* authenticated-session operations (login, `/me`, logout-all, account export/delete) behind `require_founder()`, not just the MainAI product surface | Broke legitimate pre-existing test coverage that needs two distinct logged-in accounts (RLS isolation tests, rate-limit tests, token-cleanup tests); conflated "generic account security" with "MainAI access" | Self-corrected during independent review of `claude/founder-only-launch`, this session — see D-28 |
