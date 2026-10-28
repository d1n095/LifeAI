# Production Agent Lifecycle Integration Gate — #314 `2308b697`

- **Subject:** #314 head `2308b697c6ffd4e792ffee5acccc4f4659d86389` — the composed #312→#314
  provider lineage (authenticated provider ingestion + durable ledger + verification registry
  + worker/scheduler runtime).
- **Suite:** `probes/fc_lifecycle_v1` — **13 passed, 1 xfailed(strict)** (`RESULTS_2308b69.txt`).
  DB-backed via the target's own `tests.conftest`. Loopback-only (egress guard); no paid
  calls; no production mutation; target worktree unchanged. `first_cycle_go=false`.
- **Evidence class — explicit:** provider events are **AUTHENTICATED-LOCAL** — locally
  generated and correctly HMAC-signed with a caller-held secret (`verify_v0_webhook`
  accepted, `provider_authenticated=True`). This is a genuine authenticated event path, but
  **NOT** a genuine external Cursor delivery (no `api.cursor.com` call), and it is **not**
  #314's `fixture=True` shortcut. The three classes are kept distinct throughout:
  **simulated** (`fixture=True`, #314) · **authenticated-local** (this suite) · **genuine
  external** (never performed — would need Founder live authority + real provider ingress).

## Priority 1 — Authenticated provider lifecycle: all 8 requirements PASS

Lifecycle driven through the REAL production functions
(`app.agent_coordination.provider_ingestion` + `durable` + `mainai_verification_registry`):
authorized assignment → provider binding → authenticated-local signed event → durable ledger
transition → persist/reload → independent-verification-gated continuation.

| Req | Property | Test | Result |
|---|---|---|---|
| 1 | Provider identity matches the authorized assignment + owner | PL1 | PASS — cross-owner bind `forged_rejected`; recipient-mismatch `rejected`; unbound agent `rejected` |
| 2 | Forged / replayed / reordered / cross-owner events cannot advance execution | PL1, PL2 | PASS — bad HMAC `forged_rejected` (no transition); cross-owner event `forged_rejected`; exact replay `replay_ignored` (exactly one completion); older-timestamp event `reordered_rejected` |
| 3 | STOP/revocation during execution prevents unauthorized transitions | PL3 | PASS — kill-switch active → ingest FINISHED returns `stop_rejected`/`stop_enforced=True`; obligation never reaches `completed` (STOP fenced at `assert_transition_authority` before the transition applied) |
| 4 | Crash/restart cannot create duplicate completion or spending effects | PL4 | PASS — after `persist_owner_ledger` + a FRESH `load_owner_ledger`, the restored ledger shows the obligation `completed` with exactly one provider completion; the SAME authenticated event replayed post-restart is `replay_ignored` (dedup `processed_event_ids` + `seen_event_ids` are rehydrated from the durable ledger) |
| 5 | Provider-reported FINISHED never becomes independent certification | PL5 | PASS — FINISHED → status `completed`, `independently_verified=False`, `verification_required=True`; Founder view lists it under `finished` but NOT `independently_certified`; `fabricated_progress=False` |
| 6 | Independent verification required before verification-gated continuation | PL6 | PASS — no attestation → `verification_required`; **the ordinary runtime role is DB-DENIED from writing verification records** (a compromised provider-runtime cannot forge its own attestation); builder==examiner self-cert rejected (`VerificationRegistryError`); a PASS for a different SHA does not satisfy; only a genuine independent PASS (examiner≠builder, exact SHA, written by the privileged authority) yields `attested`/`independently_verified=True` |
| 7 | One blocked agent cannot prevent unrelated authorized work | PL7 | PASS — agent A → `blocked` via authenticated BLOCKED event; unrelated agent B (different workstream) still reaches `completed`; Founder view shows A blocked and B finished concurrently |
| 8 | Founder-visible status reflects actual PERSISTED evidence, not fabricated progress | PL8 | PASS — after `persist_owner_ledger`, the Founder view built from the RELOADED ledger equals the persisted truth (completed vs blocked), `fabricated_progress=False`, `invented_vendor_access=False`, and a provider-only completion is NOT shown as independently certified |

**Security property surfaced (PL6):** verification attestations are DB-privilege-protected —
the ordinary MainAI runtime role cannot INSERT into the verification registry
(`ordinary MainAI runtime cannot create verification records`). This means provider-side
runtime code (the side that receives the FINISHED webhook) structurally cannot mint the very
independent attestation that gates continuation. Strong, not previously exercised at this
boundary.

## Priority 2 — Real runtime continuity: the autonomous operating cycle has NO production driver

| ID | Finding | Result |
|---|---|---|
| RT1 | `app.worker.Worker` is a genuine persistent loop: startup → idle poll → graceful shutdown via `request_shutdown()` (the SIGTERM/SIGINT path) | PASS |
| RT2 | Failure isolation/recovery: an exception inside `run_once` is swallowed by the poll loop and the worker keeps polling (a poll-loop bug never kills the process) | PASS |
| RT3 | **MISSING RUNTIME BOUNDARY (executable FAIL/BLOCKED):** a persistent runtime advances the operating cycle without a manual call | **xfail(strict)** — flips to a red XPASS when a driver is wired |
| RT3-evidence | Zero production callers of `run_authorized_operating_cycle` under `app/` today | PASS (pins current reality) |
| RT4 | The in-process scheduler (`start_scheduler`) registers ONLY the `token_cleanup` job — nothing advances the operating cycle, and the worker is a separate process | PASS |
| RT5 | The product itself reports `unattended_operating_cycle_ready == False` (the gap is declared, not hidden) | PASS |

**Verdict for Priority 2:** The worker runtime is real and restart-safe, but it only drains
the **execution-job** pipeline (`mainai_jobs`). The **#314 autonomous operating cycle**
(`run_authorized_operating_cycle`: goal ingestion → selection → provider dispatch → event
ingestion) has **no persistent driver anywhere in production** — the only caller in the tree
is a test. There is also **no HTTP webhook route** that calls
`ingest_authenticated_provider_event`; the authenticated ingestion path is exercised only
inside the (undriven) operating-cycle tick. Therefore, consistent with the product's own
`unattended_operating_cycle_ready=False`, this is a **manually invoked tick, not autonomous
production operation.** RT3 records that as an executable, self-invalidating requirement.

## Owning components / next wiring (for the builders; examiner does not modify product)
- **Runtime driver (MainAI/Cursor):** wire `run_authorized_operating_cycle` to a persistent
  runtime (a scheduler job or a dedicated worker handler) with STOP/spend/absence gating each
  firing; RT3 flips to PASS when a production caller exists.
- **Provider ingress (MainAI/Cursor):** add an authenticated webhook route that calls
  `ingest_authenticated_provider_event` so genuine external deliveries can reach the ledger
  (today only in-process/test callers do). The ingestion logic itself is sound (Priority 1).

## Scope / discipline
Read-only examiner; drove real production functions + the real Worker loop with
authenticated-local and in-process fixtures; no genuine external provider call, no paid
spend, no publication, no deployment, no autonomy activation; Codex/Cursor branches
unmodified; target worktree unchanged. `first_cycle_go=false`.

## Reproduce
```
probes/fc_lifecycle_v1/run.sh ../../wtcert-2308b69          # 13 passed, 1 xfailed(RT3)
```

## Next actionable blocker
Priority 3 — Codex effect-specific DB-authority candidate
`460e8ad8cbd40b7fb0d62702f28c14412688e298` (migration `0099`, ownership/epoch/privilege) —
remains **BLOCKED**: the SHA is not on the remote and no archive has been delivered. Examine
it on its exact SHA the moment the verified archive is accessible; do not repeat the already
certified STOP-clear / FA-PRIV controls.
