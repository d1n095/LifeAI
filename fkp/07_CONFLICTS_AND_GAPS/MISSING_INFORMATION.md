# Missing Information
**Rule:** Gaps listed here must not be filled with assumptions. They require real verification or a founder decision. Items resolved since FKP v1.0 have been removed from this list (see `00_MASTER_INDEX/CHANGELOG_FROM_V1.md` for what moved where) and replaced with what is genuinely still missing.

---

## Cannot verify without founder-side/dashboard access

| Item | Why missing | Required action |
|------|-------------|-----------------|
| Actual Render environment variables configured in the live dashboard | `render.yaml` documents what *should* be set; whether it actually is set is not visible from this session | Founder: check Render dashboard before any deploy |
| Whether pgvector is actually available and at what version in the live Supabase project | Reported/assumed only; last observed version (0.8.2) was from a prior session's report, not this session's direct check | Founder or a session with Supabase dashboard/CLI access: verify |
| Which AI provider API key is funded and working | Production keys are `sync: false` by design — never visible to any AI session, including this one | Founder: check provider accounts |
| Current live-deployment state of `https://lifeai-1.onrender.com` | Whatever is live there was last deployed by some prior action; this session made no deploys and cannot query Render directly | Founder: check Render dashboard, or a session with deploy-log access |

## Cannot verify without a real Supabase/production-equivalent test

| Item | Why missing | Required action |
|------|-------------|-----------------|
| What backup mechanism Supabase Free actually offers and how restoration would be tested (OD-05) | Not addressed in this FKP v1.1 pass — genuinely open, no analysis exists yet | A future session: read Supabase's current Free-tier backup/PITR terms and design a restoration drill |
| Whether the `migration-check` CI job's Postgres-service-container round-trip generalizes to Supabase's actual managed Postgres (version, extensions, permissions) | CI verifies against a disposable `pgvector/pgvector:pg16` container, not Supabase itself | Verify against a Supabase branch/staging project before relying on it as proof for a real deploy |

## Cannot determine from any source in this session

| Item | Notes |
|------|-------|
| Exact founder identifier type beyond what's already implemented (OD-01) | **Now resolved for LifeAI** — see `02_DECISIONS/DECISION_REGISTER.md` D-27. Original OD-01 framing (email vs. UUID vs. external identity) is closed by the fixed-UUID-plus-role implementation; kept here only as a pointer, not a live gap. |
| Whether MFA/passkey is required before production use (OD-02) | Analysis exists (`02_DECISIONS/OD_02_03_04_17_ANALYSIS.md`), recommendation given, but the actual founder decision to require it (and which method) is still open. |
| Founder Vault encryption/storage location (OD-03) | Analysis exists, recommendation given, founder decision still open. |
| Exact `documents`/`projects`/tasks ownership migration (OD-04) | Analysis exists, recommendation given, founder decision still open. |

## Binary/unreadable files in FKP v1.0's source material (contents still not analyzed in this v1.1 pass)

| File | Count | Notes |
|------|-------|-------|
| `.docx` files | 29 unique (by hash) | Contains LifeOS_Claude_Master_Handover, Life_Dev_Platform docs, CLAUDE_MASTER_HANDOFF. Content accessible via `python-docx` if needed. Not read in this session either — no new tooling was added to change this. |
| `.png` images | 3 unique | `IMG_0779.png`, `IMG_0780.png`, `GitHub_connector_screenshot.png`. Not analyzed. |
| `.zip` files | Many | Nested zips already extracted in FKP v1.0; the zip files themselves not further analyzed. |

These remain in `10_HISTORICAL_DOMAIN_MATERIAL/` (or, for the raw archives themselves, outside this repository — they were not copied into LifeAI, per the audit's own recommendation not to bring unrelated raw source code into this repo).
