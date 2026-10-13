# Independent examiner probes and verdict record (examiner-owned)

This orphan branch shares no history with any builder or frozen candidate branch. It contains only
examiner probe suites and this record. Nothing here is production code; it never modifies examined SHAs.
Every suite runs from OUTSIDE a detached target worktree and verifies the target is unchanged afterwards.

| Component | Exact SHA | Verdict | Suite |
|---|---|---|---|
| Grandmaster P1 fix | 6e08430a849bfb62a540d20dfa5267188e296486 | RETURN_TO_BUILDER (P1: nested paths, branch exclusivity, self-certification) | probes/grandmaster_v2 |
| Coordination store (base) | 2fbe20aacf1203fc0e16d216ef55b666b2181619 | Examination only (DB 7 PASS / 20 FAIL) | probes/coordination_v1 |
| Orchestration ledger | 32442758a4a575931c684e959f368e649fb8314b | RETURN_TO_BUILDER (baseline 23 failed / 13 passed) | probes/ledger_v2 |
| Founder Sovereignty P1-fix-2 | 4ccf1ed0aeb0ea35301a38437b57e097c16f435d | RETURN_TO_BUILDER (erasure regression; runtime-writable Founder credentials) | probes/sovereignty_v3 |
| Continuous Conversation P1-fix-4 (PR #260) | 2d9f62eb5ff3f616ecf05aed0e458cbfd1e4af39 | RETURN_TO_BUILDER (paraphrase/Recall gating, relay gaps, decision history + cross-owner definer) | probes/cc_v4 |
| MainAI self-development readiness (read-only audit) | 2fbe20aacf1203fc0e16d216ef55b666b2181619 | Examination only: 2 PASS / 6 PARTIAL / 0 MISSING; 6 minimum blockers | probes/selfdev_v1/READINESS_2fbe20a.md |
| MainAI agent-supervisor integration audit (read-only) | 2fbe20aacf1203fc0e16d216ef55b666b2181619 | Examination only: 0 PASS / 8 PARTIAL / 2 MISSING; staged minimal plan | probes/agent_supervisor_v1/AGENT_SUPERVISOR_AUDIT_2fbe20a.md |
| Path A SPEND-8/9/10 remediation #270 | d5153eb1fa1727427a24a983c005b9b88b718b2c | RETURN_TO_BUILDER: SPEND-9/10 fixed, no global mutation, concurrency clean; SPEND-8 still open: P1 cost hold vs token holds unreconciled (real cost 3.45x grant), P1 chars/3 not an upper bound (emoji prompt paid), P1 Gemini ignores max_tokens and drops thought tokens | probes/path_a_v1/VERDICT_270_d5153eb.md |
| Path A first-cycle FC-1/2/5 #269 | 66c22042ad3827aa21dff1eb158d5f719ba1f808 | RETURN_TO_BUILDER: routing + planner targets + most envelope scope HOLD (E2E-A' draft PR); P1 SCOPE-1 (path matcher has no boundary: out-of-scope file pushed + PR), P1 CAP-1 (repo_edit implies open_pr); P2 prose-trusting CTX gate, P2 Supervisor in-flight overlap | probes/e2e_v1/VERDICT_269_66c2204.md |
| Path A SPEND-7 remediation #268 | effafbebd98a8afeeca4d37b027506ed16811900 | RETURN_TO_BUILDER: SPEND-7 fixed; P1 SPEND-8 (hold does not bound the paid request; real cost 1.39x the ceiling), P1 SPEND-9 (fallback makes unreserved paid requests), P2 SPEND-10 (registry monkeypatch leak) | probes/path_a_v1/VERDICT_268_effafbe.md |
| First supervised cycle — end-to-end readiness audit | composition 2fbe20a+#264/#266(dba9390)+#265/#267(da4d96b)+#263(e33f494) | NO-GO: 6 first-cycle P1s (ROUTE, CTX x planner, SPEND-7, CI gate, envelope scope, prerequisites) | probes/e2e_v1/FIRST_CYCLE_READINESS_AUDIT.md |
| Path A P1 remediation PR #266 (Cursor) | dba9390a6ea2a167300bfd87a52922b6b5153f54 | RETURN_TO_BUILDER (9 original findings fixed; new P1 SPEND-7: paid-but-refused answers never counted against the grant) | probes/path_a_v1/VERDICT_266_dba9390.md |
| Path A activation PR #264 (Cursor) | 21ff3cc8ddd8a7da8b2c84a3219f65d752d4ea4b | RETURN_TO_BUILDER (P1 blind/truncating file writes; P1 spend gate default-off, fallback allowlist bypass, $0 settlement) | probes/path_a_v1 |
| Founder Sovereignty P1-fix-3 | 216962835b718c5a6aa306c5b03376f62d2aaee0 | RETURN_TO_BUILDER (erasure fixed; runtime-writable Founder credentials still open) | probes/sovereignty_v3 |

Reproduce, e.g.: `git worktree add --detach ../wt <sha>` then `probes/sovereignty_v3/run.sh ../wt`
(needs a local Postgres on :5433 and a venv with backend requirements; see each suite's README).
Recall identities (computed, never activated): 2fbe20a fb54de7b…, 80ecfc7 f9d1a99b…, 4ccf1ed d110adc2…, 2169628 3e9e9aeb….
