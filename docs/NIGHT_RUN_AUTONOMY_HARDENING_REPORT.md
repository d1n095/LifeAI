# Night run report — MainAI autonomy hardening (2026-08-27/28)

## PRs

| PR | Merge / head | Status | Claim |
|---|---|---|---|
| [#181](https://github.com/d1n095/LifeAI/pull/181) | `e10ae97` | **MERGED** | Supervisor spend fail-fast / defense-in-depth (Outcome B) |
| [#182](https://github.com/d1n095/LifeAI/pull/182) | `f9cedcc` | **MERGED** | Crash before settle → refuse re-invoke |
| [#184](https://github.com/d1n095/LifeAI/pull/184) | `0d12d54` | **MERGED** | Job lease expiry at Operator write effect time |
| [#183](https://github.com/d1n095/LifeAI/pull/183) | `bd04934` | **MERGED** | Heal Operator write after crash before durable audit |
| [#185](https://github.com/d1n095/LifeAI/pull/185) | `4bcc66f` | **MERGED** | Mid-flight cancel refuses future Operator effects |
| [#186](https://github.com/d1n095/LifeAI/pull/186) | `fd18f4c` | **MERGED** | Out-of-scope plan paths → CandidateValidationError |
| soak | `cursor/composed-autonomy-soak` | OPENING | Phase 8 composed Worker soak |

Current tip before soak: **`fd18f4c`**.

## Adversarial attacks completed

1–5. #181–#184 as prior (spend fail-fast, concurrent budget tip, crash-settle, write heal, lease effect)
6. Phase 6 cancel after ACCEPT / before write (#185)
7. Phase 7 governed-on-recovery — already closed (#177); no invent work
8. Phase 8 soak — local green; opens after #186

## Negative controls

Cancel-before-write, mid-driver cancel, out-of-scope path mapping all fail on pre-fix tip.

## Defects found (this continuation)

- Mid-flight cancel did not refuse mutating Operator entry points
- Out-of-scope provider path aborted Supervisor tick via uncaught `OperatorPathError` (#186)

## Unresolved / next

- Merge soak PR on exact-head CI
- Full 6-boundary cancel matrix not exhaustively one PR (partial coverage elsewhere)
- Soak follow-up: gap-repair as honest local-only zero-provider task wire

## Highest-risk next edge

Remaining founder-cancel boundaries not yet mutation-proven end-to-end, then gap-repair local-only on production_entry.
