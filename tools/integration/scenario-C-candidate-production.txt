# Freebuff integration run — /tmp/fb_fixture_c/candidate-fixture.bundle

- ✅ bundle ingested (base-seeded if needed); refs: refs/heads/candidate-fixture e3920222f78f51713976d8df66cc67a818ac00d2
- ✅ candidate e392022 resolved to e3920222f78f51713976d8df66cc67a818ac00d2 — HARD GATE PASSED
- ✅ shared ancestry with base at 69dc5ea5a9cf — merge-based integration possible
- ✅ migration graphs compatible: mine=69 revisions, theirs=68, 68 shared, 0 conflicts
- ✅ models: mine-only (must keep): ['founder_email_change.py']
- ✅ routers: mine-only (must register): ['founder_recovery.py']
- ⚠️ settings: 64 shared keys (verify no semantic drift), 0 candidate-only keys to add at composition
- ✅ frontend /api calls: 33 mine / 33 candidate, 0 candidate-only (verify endpoints exist post-composition)
- ✅ openai_consult package: zero collision with candidate tree
- ✅ candidate kill switch matches base signature: assert_not_killed() — no adaptation needed
- ✅ candidate composed at f20e55fbadcf — run integration tests before ANY further use

**RESULT: CANDIDATE PRODUCED at `/tmp/freebuff_integration/candidate` — integration tests mandatory before any further use.**
