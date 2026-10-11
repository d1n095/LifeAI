# Continuous Conversation P1-fix-4 examiner suite (examiner-owned)

Target examined 2026-10-11: 2d9f62eb5ff3f616ecf05aed0e458cbfd1e4af39 (PR #260), detached worktree, unchanged after runs.
Run: `./run.sh <target-worktree>` (local Postgres :5433; target conftest loaded as plugin; db_session = mainai_app).
Output `MATRIX|id|HOLDS/GAP|evidence`; full baseline in baseline_2d9f62e.txt (76 HOLDS / 43 GAP).

- P1-1 observations: 10 HOLDS / 2 GAP. Gaps: future-dated github_ref is "current"; malformed SHA is accepted.
- P1-2 authority gating: 33 HOLDS / 27 GAP. 24 paraphrases not gated, 10 of 16 of them Recall activation; 3 educational questions blocked.
- P1-3 relay EN/SV: 26 HOLDS / 8 GAP. 6 outbound relay asks pass, incl. "Kan du skicka testresultaten?"; 2 inbound misses.
- P1-4 decisions DB: 7 HOLDS / 6 GAP. Uniqueness and 4-way concurrency hold. Runtime UPDATE rewrites active and historical
  decisions and un-supersedes them. The SECURITY DEFINER record_founder_conversation_decision() never checks p_owner_id
  against the session, so a session for user A superseded user B's decision.
- P1-5: sibling heads off 0089_continuous_conversation identified, not composed; 0092 up/down/up schema-identical.
