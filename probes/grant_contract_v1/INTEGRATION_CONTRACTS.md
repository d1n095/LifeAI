# Cursor execution layer ↔ Codex activation/transport: integration contracts and adapter strategy

**Sources:**
- frozen #290 `6c98814` and its successor #291 `0b1871b` (Cursor; local, exact SHAs);
- #280 `37bca08` (Codex grant substrate, RETURN_TO_BUILDER);
- Codex transport spec `codex/mainai-git-transport-credential-audit` @ `bf9616e` (docs, reference probes, manifest delta);
- composition audit `9d9dc9e`.

All reads are credential-free: the local object store, the unauthenticated public API, and an anonymous git fetch with an empty credential helper. **No Codex transport implementation is published**: no `RemoteEffectExecutor` module exists in any visible branch or PR.

## 1. Contracts the Codex composition must satisfy (each is executable here)

| # | Contract | Cursor side (#290/#291) | Codex side (#280 / spec) | Executable check | Status |
|---|---|---|---|---|---|
| K1 | Grant/transport denial is a **typed** `ExternalBoundaryError` (`GrantDeniedError`), raised inside the push/PR boundary | `publication_errors.GrantDeniedError` exists; **no producer** | #280 raises `ActivationAuthorityError(RuntimeError)` | `grant_contract_v1` GC-8; `stop_classification` FO grant cases | **open**. Standalone #280 fails; any composition must pass. |
| K2 | Never raise a **bare** `RuntimeError` across the push/PR boundary | `type(exc) is RuntimeError` is re-raised as a "crash" (`execution_job.py:677`, `:774`) | any executor exception | FO-6x / RL / FO-6b | **P1 open on Cursor side** (§3): a bare RuntimeError makes governed recovery decline, then retry, re-running the paid code agent ×2 and stranding the task `ready` |
| K3 | One-use push grant, race-safe, survives restart | n/a | #280 `consume_push_grant` | GC-1, GC-2 | holds on #280 |
| K4 | Expiry, cancellation, exact bindings (job, commit, base, version) | n/a | #280 | GC-3/4/5 | holds on #280 |
| K5 | Draft PR is a **distinct** one-shot effect grant | `_handle_open_pr` has no grant check | #280 has push grants only; spec `open_pr_effect: DISTINCT_ONE_SHOT_GRANT` | GC-6 | **BLOCKED** (no API) |
| K6 | No re-issue after consumption without a **fresh preflight** | n/a | #280 mints a second grant from the same consumed receipt | GC-7 | **GAP on #280** (new) |
| K7 | Grant binds the **task branch**, compatible with #281's protected-branch CHECK | worktrees on `claude/mainai-task-*`; #281 CHECK forbids `mainai/%` | #280 requires `worktree.branch == integration branch` | `activation_280` G1 | **open** (#280 P1-4) |
| K8 | Production git never consults host credential helpers or askpass; no token in remote URL | `sanitized_git_subprocess_env` strips only `GIT_CONFIG_*`; `_authed_remote_url` embeds the token | spec §3A credential-free checkouts | `transport_contract_v1` TC-1/TC-3 (canary, sandbox, localhost 401); harness CRED-1/2/3 | **open**. TC-1 and TC-3 fail on #291; TC-2 holds. |
| K9 | Single, non-forked 0089 | `0089_activation_branch_protect` (#281) | manifest delta recommends `0089_mainai_activation_security` on 0088 | migration head check (`backend_regression` roundtrip + head listing) | **collision pending** (fold or re-point) |
| K10 | Required-check contract shape | #281: `{name, app_slug, app_id, workflow_path, events}` | #280 envelope accepts only `{name, app_slug}` | `activation_280` E2 | **open** |
| K11 | Consume **before** network; ambiguous effects observe the exact ref and never replay | #291: a durable `finalized` checkpoint proves the push; recovery fetch failures are `unsafe_to_auto_recover` | spec §4 | RL / FO-6x (replay via retry); recovery suites | partially. #290/#291 recovery inspection is sound, but the governed retry path still replays (K2). |
| K12 | Erasure covers grants and receipts | n/a | #280 DELETE triggers block erasure | `activation_280` E1/E1b; ER-4 (full cycle, passes on #290) | **open** (#280 P1-1) |

## 2. Adapter strategy (implemented: `grant_contract_v1/grant_adapter.py`)

1. **Discovery, never assumption.**
   - `discover()` first probes the module names the spec implies (`remote_effects`, `remote_effect_executor`, `transport`, `git_transport`).
   - If one exists but no adapter has been written for it, the result is `AdapterNeeded`, which is a **blocker**.
   - It falls back to #280's `activation_security`. If neither exists, the result is `CapabilityMissing`, which reports as **BLOCKED**.
   - The tests **fail** (never skip) on both outcomes.
2. **Fixed assertions.** GC-1..GC-8 live in `test_grant_contract.py` and call only the adapter surface:
   - `build_context`, `mint(effect_kind=…)`, `consume(effect_kind=…, overrides)`, `cancel`, `denial_types`.

   An adapter translates calls and owner scoping (RLS). It never catches or reinterprets a denial.
3. **Validated against real code.** On #280 `37bca08`:
   - GC-1..GC-5 pass (8 tests);
   - GC-6 is BLOCKED (no PR grant);
   - GC-7 is a GAP (re-mint from a consumed receipt);
   - GC-8 is BLOCKED (no typed boundary standalone).

   This proves the adapter drives a real grant API and that the assertions discriminate.
4. **Real-origin injection when the transport lands.** The `stop_classification` FO grant cases currently inject #280's denial texts at `execution_job.push_worktree_branch`. When Codex's executor replaces that seam, add a ~20-line injector that makes **Codex's own grant gate** deny, for example by expiring or cancelling a real grant through the adapter. The FO assertions (terminal `failed`, zero extra paid calls, one job) stay unchanged.
5. **Bindings vocabulary** follows the spec's `effect_bindings`. `GrantContext.extra` carries the candidate-specific identifiers.

## 3. Correction to earlier evidence (FO-6)

My #288/#289/#290 reports stated that FO-6 (a bare RuntimeError at the push boundary) was "contained: no paid call, no re-execution, Founder-visible recovery record". **That was wrong**, caused by a defect in my harness:
- my fake GitHub `get_ref` raised a raw subprocess error for a missing branch;
- the real client raises `GitHubClientError(404)`;
- the raw error made recovery inspection crash on every tick ("failed to auto-recover"), which masked the real behaviour.

With a faithful 404 (`_harness.py` fixed), on #288, #289, #290 and #291 identically:
- recovery classifies `LOCAL_COMMITTED_NOT_PUSHED`;
- the goal is envelope-governed, so `_decline_takeover_for_governed_goal` declines the takeover;
- the task becomes `retryable_failed`;
- the retry tick re-dispatches a fresh job, which **re-runs the paid code agent**;
- this repeats up to `max_attempts`: **3 jobs, 3 code-agent calls (2 extra paid)**, after which the task is **stranded `ready`**. It is not terminal and not visible to the Founder as a failure.

#290's one-takeover cap counts `takeover_job_id`, which governed goals never set, so it **never engages on Path A**. #291's recovery-classifier changes do not touch this path. This is also the behaviour after a genuine worker crash at the publication boundary, which is an ambiguous effect that the spec says must be observed, never replayed.

**Re-rating:** FO-6 is **P1 for first-cycle activation** (Cursor-owned). It is pre-existing, not introduced by #288–#291. Their scoped verdicts stand for what they delivered. The executable gate under the corrected harness fails on FO-6 for all four.

## 4. Remaining blockers (first-cycle GO = false)

| # | Blocker | Owner | Gate |
|---|---|---|---|
| 1 | **FO-6:** at the publication boundary, any non-typed exception or crash must fail closed or observe the exact ref. Never retry-with-re-run on governed goals; never strand `ready`. | Cursor | FO-6x, RL-1..3, FO-6b, `untyped_boundary_exception` |
| 2 | K1, K5, K6, K7, K10, K12 and the #280 P1s in a published Codex composition | Codex | GC-1..GC-8, `activation_280` |
| 3 | K8: credential-free transport (TC-1/TC-3, CRED-1/2/3) | Codex | `transport_contract_v1`, harness CRED rows |
| 4 | K9: single 0089 | Codex + Cursor composition | migration head check |
| 5 | TA-12/TA-13/PUB-2 publication content; CI-6 | Codex / #281 lane | harness rows, `task_authority` |
| 6 | Founder/GitHub class-F facts | Founder | MATRIX.md |
