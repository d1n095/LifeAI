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
| Founder Sovereignty P1-fix-3 | 216962835b718c5a6aa306c5b03376f62d2aaee0 | RETURN_TO_BUILDER (erasure fixed; runtime-writable Founder credentials still open) | probes/sovereignty_v3 |

Reproduce, e.g.: `git worktree add --detach ../wt <sha>` then `probes/sovereignty_v3/run.sh ../wt`
(needs a local Postgres on :5433 and a venv with backend requirements; see each suite's README).
Recall identities (computed, never activated): 2fbe20a fb54de7b…, 80ecfc7 f9d1a99b…, 4ccf1ed d110adc2…, 2169628 3e9e9aeb….
