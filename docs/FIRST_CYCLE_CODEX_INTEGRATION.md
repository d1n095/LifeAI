# Integrating the eventual Codex activation/transport candidate

This staging candidate (`cursor/first-cycle-staging-91c1`) already contains frozen `#263`,
`#276`, `#281`, `#282`, and provisionally `#284`. It does **not** contain `#280` and does
**not** implement Git transport, remote-effect grants, or worktree credential isolation.

Do not modify Codex's active branches from this lane. Do not add a second grant or
transport system here.

## Exact conflicts with `#280` @ `37bca08a287f00b6282222047340bffa949ff788`

Merge-base with this candidate: `c56e0757cc117b443b377b987743ce2364558497` (`#272`).
`#280` does not contain `#273`/`#276` Path A task-authority or `#281` activation-readiness.

| Path | This candidate | `#280` | Compose rule |
|---|---|---|---|
| `backend/alembic/versions/0089_mainai_activation_branch_protection.py` | revision `0089_activation_branch_protect`, down `0088_erasure_completion_phase` | absent | Keep. After Codex's accepted migration lands, re-point `down_revision` to that revision **or** fold the two `ck_mainai_task_worktrees_branch_not_protected` DDL statements into Codex's single 0089/00NN file and drop this file. Never merge two children of `0088`. |
| `backend/alembic/versions/0089_mainai_activation_security.py` | absent (must stay absent until PASS-reviewed) | revision `0089_mainai_activation_security`, down `0088` | Import only from a future PASS-reviewed replacement, then linearize as above. |
| `backend/app/config.py` | `mainai_activation_mode`, `mainai_activation_approved_base_sha`, Path A GitHub bases, spend caps | `mainai_activation_sha`, `mainai_activation_integration_branch`, token/ruleset/founder attestation digests | Union the fields. Do not rename or replace the Path A fail-closed activation-mode pair. Empty Codex attestation values must remain NO-GO. |
| `backend/tests/backend/test_config_contract.py` | documents Path A activation fields | documents Codex attestation fields | Union `CONFIG_MATRIX` entries. |
| `backend/tests/backend/test_migration_roundtrip.py` | asserts a **single** Alembic head | same assertion on a forked 0089 | Must stay a single head after composition. |
| `.env.example` | Path A `MAINAI_ACTIVATION_MODE` / approved SHA | Codex attestation placeholders | Union comments; defaults stay empty/false. |
| `backend/app/mainai_execution/activation_security.py` and envelope/router/model files | absent | new substrate | Add after PASS review. Do not duplicate into `review_receipt.py` or `required_checks.py`. |
| `backend/app/execution_envelopes/service.py` | Path A envelope routing / authorized paths | activation binding columns + immutability trigger | Keep Path A behavior; add Codex columns/trigger without widening Path A write authority. |

`#280` is **RETURN_TO_BUILDER**. Do not compose that SHA. Wait for Codex's replacement and an independent PASS.

## Git transport / grants (`codex/mainai-git-transport-credential-audit` @ `bf9616e`)

That SHA is documentation and reference probes only. Overlap with this candidate is
`docs/BRANCH_REGISTRY.md` plus three new docs/evidence files.

Production gaps that must remain FAIL/BLOCKED on this candidate until a frozen Codex
implementation is composed:

- CRED-1 `_authed_remote_url` embeds the token
- CRED-2 worktree `.git/config` stores the token
- CRED-3 worker/test/reviewer subprocesses can read writable remote credentials
- E2E-3 `push_worktree_branch` is not grant-gated
- GRANT-PROD production push/PR grant interface missing
- TA-12/TA-13/PUB-2 publication history/symlink/gitlink (Codex-adjacent publication oracle)

Compose rule: take Codex's implementation onto **this staging tip**, not onto `#272`/`#280`.
Preserve `evaluate_first_cycle_review_receipt`, `stop_first_cycle`, and
`should_suppress_paid_replan`. Reviewers still must not receive push authority.

## Required-check contract

Keep the 12-name `ACTIVATION_REQUIRED_CHECKS` set, including `Frontend — npm audit`.
`#282`'s policy is the only `check-npm-audit.js`. Do not restore `#275`'s pre-A6/A7 script.
`#281`'s `mainai/**` triggers and `permissions: contents: read` stay.

## Activation branch

The worktree/PR base in activation mode remains exactly
`mainai/integration-founder-supervised-v1`. Do not create, push to, or merge into that
branch from this composition. Track B stays off. No paid providers, Recall, or GitHub
settings changes.
