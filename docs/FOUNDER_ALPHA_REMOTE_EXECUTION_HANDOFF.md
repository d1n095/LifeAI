# Founder Alpha remote execution handoff

Snapshot: 2026-11-13. GitHub is the authoritative source. The current Codex execution host is the Founder's physical Mac (`MacBook-Air-som-tillhor-Dennis.local`, Darwin arm64), so no further LifeAI builds, tests, databases, containers, model inference, indexing, services, or development workloads may run there. `first_cycle_go=false`.

## Active security task

The active implementation is the durable Blueprint pre-dispatch receipt correction in PR #384.

| Scope | Branch | Exact commit | Tree / dependency | State |
| --- | --- | --- | --- | --- |
| Durable Blueprint egress receipt | `codex/blueprint-egress-durable-receipt` | `b857e095191bdd1353d46761b2943e892aa70002` | tree `9c81825dddf519d2748152d10ea08264d6b32fb7`; parent #360 `3053ce3149207b6cc7476b75b602df643afcfb75` | independent no-defect verdict at exact SHA; composition required |
| Historical standalone `0069` compatibility | `codex/founder-alpha-0069-migration-compat` | `8ac278d5d90c4c5b550aad6741f4bcb9fd193725` | isolated child of frozen #352 `07ce4a1d02d0eaa326acb53c0cb1d0a4708ee765` | RETURN TO BUILDER for value-round-trip and damaged-state recovery |
| Immutable Ollama CI installer | `codex/founder-alpha-ollama-ci-pin` | `4b27c67570b0335fe7a0fd28027596a984a56f87` | isolated child of frozen #352 | independent no-defect verdict at exact SHA |
| Gate/first-start evidence | `codex/founder-alpha-final-gate-package` | this document's branch head | evidence only | review package; no execution authority |

## Remote executable evidence

- PR #384: GitHub Actions runs `43225833447` and `43225792239` PASS. Backend jobs `131208643137` and `131208504666`: 2,379 passed, 1 skipped. Alembic, RLS/session, account, frontend TypeScript/ESLint, Docker/Vercel builds, npm audit, Playwright full-stack, and same-origin proxy passed.
- PR #363: runs `43222624957` and `43222636109` PASS. Backend jobs `131197815396` and `131197755066`: 2,374 passed, 1 skipped; historical PostgreSQL upgrade regression passed.
- PR #382: ordinary run `43223172075` PASS; backend job `131199813380`: 2,374 passed, 1 skipped. Real-local evidence runs `43223186314` and `43223179331` PASS. These are prior GitHub-hosted evidence, not permission to run Ollama locally now.

## Independent review handoff

Claude independently examined #384 and #382 at their exact frozen commits and found no defect. #363 remains a builder task and must later be examined by a different reviewer.

1. #384 (completed, comment `6850901877`): verified the owner-bound content-free receipt is committed and independently visible before provider invocation; receipt survives transport failure, empty response, and later business rollback; receipt failure prevents provider invocation; inherited NEVER_EGRESS and redaction invariants remain intact.
2. #363 (open): reproduce fresh install and historical exact-`0069` upgrades; verify the serial migration ancestry, provider-disclosure preservation, embedding provenance, downgrade behavior, and a single head.
3. #382 (completed, comment `6850967538`): verified the pinned Ollama release identity, checksum-before-extraction behavior, exact-SHA checkout, architecture restriction, and absence of mutable installer execution.

## Remaining blockers

- #363 lacks remediation and independent PASS for populated provenance-value round trip and damaged dual-version recovery.
- #237 and Founder Alpha currently reuse exact Alembic revision `0069` for different schema effects; deterministic composition is impossible until explicitly reconciled.
- The three isolated corrections have not been deterministically composed into a new Founder Alpha integration candidate.
- The current Codex session has no authorized remote development compute environment. GitHub Actions can verify pushed commits but is not an interactive implementation environment.
- No merge, deployment, provider activation, Recall activation, real-file ingestion, or first-cycle authorization exists.

## Local preservation inventory

The active `/private/tmp/lifeai-blueprint-egress-durable-receipt` worktree is clean and its exact commit is present on GitHub. Older `/private/tmp/lifeai-*` directories, `/private/tmp/mainai-worktrees`, and `lifeai-activation-authority-substrate` are deliberately preserved: several contain evidence, broken worktree pointers, or ownership-ambiguous state. No persistent directory may be deleted without explicit path-specific Founder authorization.

Original Founder documents and future ingestion sources are outside this cleanup scope and must remain unchanged.

## Resume condition

Implementation resumes only in an explicitly authorized remote compute environment, from the exact GitHub branch/SHA selected after checking current ownership. The next product step after independent reviews is a deterministic integration child combining only approved corrections, followed by exact-SHA GitHub Actions and independent certification.
