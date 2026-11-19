# Z.ai Round 12 (Cognitive Core) — integration status: BLOCKED on the artifact

Recorded 2026-11-19 on `freebuff/founder-alpha-ops-recovery-20261118`.

Z.ai reported Round 12 at `cc4d2804` with a release package
(`lifeai-cognitive-core-r12-release/`: Git bundle, tree snapshot, `SHA256SUMS`,
`VERIFY_AND_IMPORT.sh`, compatibility spec, integration instructions, cognitive acceptance
evidence). None of it is treated as present until the commit can be read on this host.

**Status: not integrated. Nothing was reconstructed.** This document is the exact transfer
requirement and the reviewed plan that runs the moment the artifact arrives.

## Probes performed (2026-11-19)

| Probe | Result |
|---|---|
| `git cat-file -t cc4d2804` | `fatal: Not a valid object name cc4d2804` |
| `git log --all` for the SHA | no match; no `zai` / `r12` / `cognit` ref |
| `git ls-remote origin` | no ref carrying `cc4d2804` |
| project directory `lifeai-cognitive-core-r12-release/` | absent |
| `find` for `*.bundle`, `SHA256SUMS`, `*round12*`, `*cc4d2804*` | no artifact on this host |

Z.ai's sandbox filesystem is not shared with this machine. A "similar branch" is not a
substitute: the commit id must match.

## Exact transfer requirement (any one)

1. `git bundle create lifeai-cognitive-core-r12-release.bundle <range>` containing `cc4d2804`,
   plus its SHA-256 and the bundle's own `git bundle verify` output;
2. a `git format-patch` series whose cover letter names the produced SHAs;
3. a pushed scratch ref in `d1n095/LifeAI` (for example `refs/zai/r12`).

## Migration collision — now confirmed against real files

This tree's head is `0079`, and the numbering range Z.ai reported **already exists here with
different migrations**:

| Revision | This tree (applied, reviewed) | Z.ai R12 (reported) |
|---|---|---|
| `0075` | `0075_system_activation_authorization.py` | claimed by R12 |
| `0076` | `0076_github_evidence_staging.py` | claimed by R12 |
| `0077` | `0077_calendar_foundation.py` | claimed by R12 |
| `0078` | `0078_repository_file_source_units.py` | — |
| `0079` | `0079_audit_log_append_only.py` (head) | — |

So the collision is by **identity, not only by number**. `alembic heads` currently reports a
single head (`0079`). R12's migrations must be re-based onto `0079` with a reviewed merge
migration, and the merge migration must be tested against a database that already has
`0075`–`0079` applied (this repository's real state, not an empty one).

Forbidden: renumbering `0075`–`0079`, editing applied migrations, or dropping and recreating
`alembic_version`. Each of those rewrites history that production databases have already run.

## Integration plan (prepared, not executed)

1. **Identity and integrity.** Verify `SHA256SUMS`, `git bundle verify`, and that `cc4d2804` is
   the delivered head. Refuse substitution or a partial bundle.
2. **Isolation.** Branch `freebuff/zai-r12-integration-<date>` from the current head. No merge
   into the default branch, no force-push over Z.ai's branch, no touching frozen PRs #459/#460.
3. **Common ancestor and full diff.** `git merge-base`, then review the entire diff — not only
   the files that conflict.
4. **Migration DAG.** AST-parse every added revision for `revision`/`down_revision`; build the
   graph; add a merge revision on top of `0079`; run `alembic upgrade head` on a copy of a
   database that is already at `0079`, then `bootstrap_database.py --verify-only`.
5. **Domain reconciliation.** Persistent memory, decisions, RLS policies, model providers,
   research pipelines, agent execution — each compared for two implementations of one concept.
6. **Prompt fencing — one canonical boundary.** This tree's implementation is
   `backend/app/rag/prompt_fencing.py` with `backend/tests/security/test_prompt_injection_fencing.py`
   (7 tests) and it is a policy-locked control. Z.ai's C1–C7 guarantees must be compared line by
   line against it. Keep exactly one implementation; if Z.ai's is strictly stronger, migrate the
   callers deliberately and prove equivalence. Two fences is a security regression, not a merge.
7. **Conversation path.** Wire the Cognitive Core into the real MainAI chat path and prove it with
   a real model answer, not a unit test of the core in isolation.
8. **Cognitive state contract.** Expose only a versioned, non-sensitive state to the Living Core;
   the Living Core must stay independent of security and cognitive decision layers.
9. **Combined acceptance.** Extend
   `backend/scripts/acceptance/founder_release_acceptance.py` item
   `4.cognitive_reasoning` from `external_blocked` to a real check (premise challenge and
   reasoning trace against the wired path), then re-run the full gate.

## What is verified today without R12

The release gate reports item `4.cognitive_reasoning` as `blocked` and every other item
`passed` — 13 of 14. `ready_without_cognitive_core: True`, `full integration: False`.
See `docs/FOUNDER_RELEASE_CANDIDATE.md` for the exact evidence.

There is no cognitive/premise module anywhere in this tree (`grep -rln premise backend/app`
returns only unrelated transcription and storage matches), so no claim of cognitive reasoning
is made and none is simulated.
