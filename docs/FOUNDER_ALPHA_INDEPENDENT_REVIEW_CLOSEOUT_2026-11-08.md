# Founder Alpha Independent Review Closeout

**Review date:** 2026-11-08
**Overall verdict:** **FAIL / NOT READY**
**Startup gate:** `first_cycle_go=false`

This report records exact-SHA evidence for the Founder Alpha remediation candidates and
Claude's WAITING_PROVIDER correction. It is an independent review checkpoint, not formal
security acceptance. No candidate code was changed; no production database, production
secret, merge, deployment, or startup authorization was used.

## Exact-SHA verdicts

| Candidate | Exact head | Exact tree | Verdict and scope | Executed evidence |
|---|---|---|---|---|
| #358 WAITING_PROVIDER | `c7adaffeee156a0e4373908e7fc6981da3d9759e` | `c4340e60e4cbc7d94425e223c4683d0471e33aad` | **PASS, scoped** for retry floor, zero-base semantics, multi-worker recheck/lease behavior, and regression coverage. Not full release approval. | Local exact-SHA run: 11 focused tests passed. Backend CI job passed in [run 42485564040](https://github.com/d1n095/LifeAI/actions/runs/42485564040/job/128746544338). npm audit and both Playwright E2E jobs failed. |
| #360 Blueprint egress remediation | `3053ce3149207b6cc7476b75b602df643afcfb75` | `82fb013874c5edb30da4f762fdede6e373d10794` | **FAIL.** Source gate, owner binding, denial-before-dispatch, and redaction work in synthetic tests; allowed-call audit receipt is not durable on provider error or empty result. | Focused local exact-SHA synthetic run: 5 passed, 2 failed at receipt-durability assertions; secret/log check separately passed. CI runs [42489220833](https://github.com/d1n095/LifeAI/actions/runs/42489220833) and [42489176923](https://github.com/d1n095/LifeAI/actions/runs/42489176923) passed. |
| #363 historical migration compatibility | `1d3fcb0440aca0f460330ecacfb99c15809665fa` | `cc1b68315bc2b6582088c70302f4654e670cb862` | **FAIL / BLOCKED.** Historical standalone-0069 upgrade and populated provenance-value round-trip pass; latest no-op migration round-trip assertion fails. Damaged-state recovery remains NOT VERIFIED. | Local disposable-Postgres runs: historical test passed; populated provenance test passed; latest round-trip failed. Backend and aggregate also fail in [42489636634](https://github.com/d1n095/LifeAI/actions/runs/42489636634) and [42489615897](https://github.com/d1n095/LifeAI/actions/runs/42489615897). |
| #364 pinned Ollama installer | `6c6fa215c37cf444f6c68c4c1ef6ab51c4bfd9fb` | `9777816d69a4b2cc4baf26f42e6055174a4a1200` | **PASS, scoped** for installer artifact integrity; **NOT VERIFIED stable** for real-local inference reliability. | Local synthetic workflow-integrity tests: 4 passed. Normal CI [42489984734](https://github.com/d1n095/LifeAI/actions/runs/42489984734) and full real-local acceptance [42490481184](https://github.com/d1n095/LifeAI/actions/runs/42490481184) passed. Prior [42490072698](https://github.com/d1n095/LifeAI/actions/runs/42490072698) installed Ollama successfully but chat inference failed at its 120-second timeout. |

Earlier frozen candidates retain their previously published verdicts: #340 **FAIL** for
the Blueprint source-egress bypass; #352 **FAIL** inheriting that bypass and the mutable CI
installer; #349 passed its isolated security scope but had a migration integration issue;
#351 passed its reviewed security scope, with CI evidence incomplete. Those historical
verdicts do not approve their descendants or replacements.

## Finding FA-360-1 — disclosure receipt may roll back after external disclosure

- **Severity:** P2 / MEDIUM; **confidence:** 8/10.
- **Affected exact SHA:** #360, `3053ce3149207b6cc7476b75b602df643afcfb75`.
- **Affected code:** `backend/app/routers/blueprint.py:257-285`; disclosure flush in
  `backend/app/providers/registry.py:199-217`; request-session cleanup in
  `backend/app/db.py:54-59`.
- **Expected:** Once egress authorization permits an external call, a durable, owner-bound,
  content-free disclosure receipt exists before provider dispatch and remains auditable if
  the provider fails, returns an empty response, or the request/application transaction
  later rolls back.
- **Actual:** The allowed disclosure row is flushed but remains in the request transaction
  while the provider is called. The `ProviderError` and empty-response paths return HTTP 503
  without committing the receipt. Closing the request session rolls it back. A provider may
  have received source text before a timeout while a fresh session finds no durable event.
- **Reproduction:** Use an authorized synthetic Founder request with an indexed synthetic
  source. Make the fake external provider record that it was invoked and then raise a
  transport error. After the route returns 503, query `provider_disclosure_events` from a
  fresh session. Expected after remediation: the owner-bound `allowed` attempt receipt is
  present and contains hashes/metadata only. Current source flow predicts it is absent.
- **Evidence level:** Reproduced at the exact SHA on disposable PostgreSQL using temporary,
  uncommitted synthetic tests. A fake external provider inspected the disclosure ledger from
  an independent session at invocation time: policy authorization had occurred and provider
  dispatch proceeded, but no durable receipt was visible. After a synthetic transport error
  returned HTTP 503, a fresh session still found no allowed receipt. A separate fake
  provider returned an empty response; the route returned HTTP 503 and again no receipt was
  durable. No provider credentials or real service were used.
- **Remediation:** Persist a content-free attempt receipt durably before invoking an
  external provider, using a transaction boundary that survives rollback of the
  Blueprint/request transaction. If receipt persistence fails, fail closed and do not call
  the provider. Record the eventual outcome separately without storing raw prompt/source
  content. Avoid committing unrelated request writes as a side effect.

### Prepared synthetic regression

The following pytest test is designed to be added to
`backend/tests/backend/test_founder_alpha_blueprint_e2e.py` on a remediation candidate. It
uses only the existing synthetic provider and database fixtures. It is expected to fail on
#360 until the receipt transaction is made durable. An equivalent test was executed at the
exact SHA in an isolated worktree and failed at the receipt assertion; the temporary test
was not committed to or left in the candidate checkout.

```python
def test_blueprint_provider_failure_keeps_durable_disclosure_receipt(
    client, superuser_db, _fake_model
):
    founder_id = _founder_id(superuser_db)
    _make_indexed_source(
        superuser_db,
        founder_id,
        "Synthetic failed-provider source",
        text_value="Synthetic non-secret project excerpt.",
    )
    _fake_model["fail"] = True

    response = client.post(
        "/api/blueprint/drafts",
        json={"title": f"Provider failure {uuid.uuid4().hex[:8]}", "query": "Synthetic source"},
        headers={"X-CSRF-Token": _login(client)},
    )

    assert response.status_code == 503
    assert _fake_model["calls"] > 0
    receipt = (
        superuser_db.query(ProviderDisclosureEvent)
        .filter_by(
            owner_id=founder_id,
            purpose="blueprint_synthesis",
            decision="allowed",
        )
        .order_by(ProviderDisclosureEvent.created_at.desc(), ProviderDisclosureEvent.id.desc())
        .first()
    )
    assert receipt is not None
    assert receipt.attempted_content_hash
    assert receipt.sent_content_hash
    assert "Synthetic non-secret project excerpt." not in repr(receipt)
```

Run together with the existing synthetic policy tests on the candidate:

```sh
cd backend
pytest -q tests/backend/test_founder_alpha_blueprint_e2e.py \
  -k 'synthesis_failure_rolls_back_no_partial_draft or blueprint_provider_failure_keeps_durable_disclosure_receipt or blueprint_forbidden_retrieved_source_never_reaches_external_provider or blueprint_retrieved_source_secrets_are_redacted_before_external_provider'
```

The existing denial test verifies a `NEVER_EGRESS` marker and a synthetic private-key
marker never invoke `OpenAIProvider.chat`, that the denied receipt is recorded, and that no
draft is created. The existing secret-redaction test uses a synthetic API-key pattern,
checks that the external provider receives `[REDACTED]`, and checks the audit row stores
hashes/redaction categories rather than the original key. A temporary exact-SHA extension
also asserted the synthetic credential and source text were absent from captured logs and
the serialized event; it passed. The outer-request-rollback-after-authorization scenario
remains untested. All temporary tests were isolated to the exact-SHA worktree and removed
after execution.

**Executed exact-SHA evidence (disposable PostgreSQL):** focused tests covering the existing
provider-failure/no-partial-draft case, both `NEVER_EGRESS` and private-key denial variants,
the existing redaction case, a fresh-session durable-denial/log assertion, and synthetic
provider-error plus empty-response durability checks produced **5 passed and 2 failed**.
The two failures are the expected-receipt assertions after provider error and empty response.
The provider-error test additionally observed no durable receipt from a second session at
the instant the provider was invoked, distinguishing authorized dispatch from successful
completion. A separately strengthened secret test passed and asserted the synthetic API key
and sensitive document text were absent from the external payload, disclosure row, and
captured logs. These temporary adversarial tests were not committed to the frozen candidate.

The executed focused selector was:

```sh
cd backend
pytest -q tests/backend/test_founder_alpha_blueprint_e2e.py \
  -k 'synthesis_failure_rolls_back_no_partial_draft or blueprint_forbidden_retrieved_source_never_reaches_external_provider or blueprint_retrieved_source_secrets_are_redacted_before_external_provider or review_failure_receipt_survives_provider_error_in_fresh_session or review_empty_provider_response_keeps_durable_disclosure_receipt or review_denial_receipt_is_durable_and_secret_stays_out_of_logs'
```

The last three `review_*` test functions were temporary synthetic tests. The secret/log
extension was then rerun separately by selecting
`blueprint_retrieved_source_secrets_are_redacted_before_external_provider` and passed.

Successful Ollama calls are classified `local`, not external. If Ollama fails and an
external fallback is configured, the provider chain may try that fallback after another
owner-aware egress check. Deployments requiring strict local-only processing must not
configure external fallback; a local-only acceptance test must prove zero external calls
for both successful and failing local-provider paths.

## Finding FA-363-1 — migration round-trip contract fails

- **Severity:** P2 / MEDIUM release blocker; this is a migration/compatibility failure, not
  a demonstrated exploitable vulnerability.
- **Affected exact SHA:** #363, `1d3fcb0440aca0f460330ecacfb99c15809665fa`.
- **Affected test:** `backend/tests/backend/test_migration_roundtrip.py:340`,
  `test_latest_migration_upgrade_downgrade_upgrade_round_trip`.
- **Exact transition and actual result:** the test downgrades from
  `0073_provider_disclosure_local` to `0072`, then upgrades back to `0073`. Because 0073 is
  intentionally a no-op marker in this candidate, the schema snapshot does not change on
  downgrade; the generic test asserts that it must change and fails. Backend and aggregate
  CI fail in runs 42489636634 and 42489615897. The historical-0069 regression itself passes
  in 42489636634.
- **Expected:** The test and migration contract explicitly distinguish a no-op revision
  from a schema-changing downgrade while continuing to detect broken downgrade behavior
  for revisions that do change schema. The standalone historical `0069` upgrade should
  reach the single current head while preserving pre-existing data.
- **Verified:** Fresh full-chain downgrade-to-base/upgrade-to-head; synthetic populated
  standalone-0069 upgrade; final embedding identity columns; seeded local disclosure row
  and hash fields. In an additional disposable-Postgres test at the exact SHA, a synthetic
  chunk with non-NULL `embedding_model="synthetic-model-identity"` and
  `embedding_native_dim=768` survived downgrade to `0069` and upgrade to head unchanged.
- **NOT VERIFIED:** Recovery of the already-damaged #352 state where the version table
  contains both `0069` and `0073` but `document_chunks` lacks identity columns. This specific
  state was not created or repaired in the local test; do not claim automatic repair or
  infer unknown provenance.

  Executed against a local disposable PostgreSQL 16 container using the exact #363 checkout:

  ```sh
  pytest -q tests/backend/test_migration_roundtrip.py \
    -k 'historical_standalone_0069_upgrades_without_skipping_embedding_identity or latest_migration_upgrade_downgrade_upgrade_round_trip'
  pytest -q tests/backend/test_migration_roundtrip.py \
    -k 'review_non_null_embedding_identity_survives_0069_downgrade_upgrade'
  ```

  The second selector was a temporary synthetic test added only to the isolated worktree,
  then removed; it verifies the actual non-NULL model/dimension values after the migration
  cycle rather than merely checking that the columns exist.
- **Remediation:** Correct the no-op round-trip assertion without weakening meaningful
  migration checks; add tests for populated non-NULL identity values across downgrade and
  upgrade; separately design and test a data-preserving recovery procedure for damaged
  databases. Do not edit historical applied migrations or rewrite migration history.

## Other candidate evidence and status

### #358 — WAITING_PROVIDER backoff

The retry helper clamps full-jitter delay to `min(MIN_SECONDS, base)`, giving the
production 30-second base a 5-second minimum while preserving explicit `base=0` wake-now
semantics. Tests cover low/mid/high jitter, exhaustion, zero/low base and wake behavior.
Due-wake handling locks/refreshes the row and rechecks status and retry time before
transition. The multi-worker lease scenario remains covered by the existing composed
autonomy soak. Independent scoped verdict: **PASS**. Exact-SHA tests rerun locally against
disposable PostgreSQL: `test_provider_wait_backoff_floor.py`,
`test_waiting_provider_wake_release.py`, and
`test_composed_autonomy_soak_v4_long_autonomy_gap_restart_lease` — **11 passed**.

The PR remains CI-red in [run 42485564040](https://github.com/d1n095/LifeAI/actions/runs/42485564040):
frontend npm audit reports unallowlisted high/critical advisories; full-stack and
same-origin Playwright fail to install Chromium with Playwright 1.56.1 on
`ubuntu26.04-x64`. The PR changes only the backend retry helper and its test, not frontend
dependencies or CI configuration, so these failures are unrelated to the #358 diff. The
run at its exact base SHA `64c07d5b0c4821bfb175700283021aa6e95cb61c` was cancelled, so this
review cannot independently assert that the same check failures were observed on that exact
base. They remain release checks to resolve, not defects attributed to #358.

### #364 — pinned Ollama installer

Both `founder-alpha-verification.yml` and `real-local-ai-verification.yml` pin the
v0.40.15 Linux amd64 release asset and verify SHA-256
`628c018404a4206493efd38e46251c154ede5141b8b8dc85c659b3b027c47379` before extraction.
The digest matches the GitHub release API asset digest; corrupted-artifact tests confirm
failure before extraction. The exact-SHA local synthetic workflow-integrity tests passed
**4/4**; normal CI and real-local pre/post-restart acceptance passed.

The earlier real-local run [42490072698](https://github.com/d1n095/LifeAI/actions/runs/42490072698)
passed the installer/model setup step (14:30:25–14:30:50 UTC), then failed during
pre-restart real-model chat after indexing. Its uploaded Ollama log records `/api/chat`
returning HTTP 500 after exactly 2m, cancellation of the generation task after about 4,400
generated tokens, and the backend's Ollama request failing as unreachable. The reviewed
`ollama_provider.py` uses a default 120-second HTTP timeout. This is strong evidence of a
chat-inference timeout, **not an installer failure**. A later run
[42490481184](https://github.com/d1n095/LifeAI/actions/runs/42490481184) passed grounded
retrieval, real-model Blueprint generation/versioning, restart persistence, and Founder
approval. One failed inference run followed by one successful run does not establish stable
latency; keep this reliability risk open. Model pulls remain registry-tag-based rather than
content-digest-pinned, outside the installer-integrity review.

## Codex remediation handoff and independent acceptance criteria

1. **Blueprint audit integrity:** Add a durable, content-free pre-dispatch receipt boundary
   and outcome handling. Pass the prepared failed-provider test, the `NEVER_EGRESS`
   no-provider-call test, secret-redaction/log-scrubbing tests, and an outer-rollback
   persistence test. Independently review the resulting exact SHA; the implementer must not
   self-certify.
2. **Migration compatibility:** Resolve the no-op 0073 round-trip failure, run the full
   backend suite green, test populated provenance preservation, and validate a distinct
   recovery procedure on disposable data matching the damaged dual-version state. Keep
   recovery `NOT VERIFIED` until that specific test passes; do not rewrite historical
   migrations.
3. **#358 CI:** Clear required npm-audit and Playwright runner failures before calling its
   PR fully green. Preserve its scoped backend behavior verdict separately.
4. **#364 installer:** Keep the fixed version and digest verification. Track the prior
   grounded-retrieval failure and model-tag immutability as separate follow-ups.
5. **Final gate:** Update the decision package with new exact SHAs, CI links, and
   independent findings. Keep the original #340/#352 frozen verdicts visible; a correction
   candidate does not retroactively approve a frozen SHA.

## Final executable acceptance matrix

“Controlled synthetic startup” below means an isolated, disposable environment with
synthetic documents and credentials, no production data, no paid provider, and no
authorization to activate or deploy. Production deployment remains a separate, stricter
gate. Tree IDs identify the exact source reviewed, not a mutable branch name.

| PR / exact SHA / tree | Verdict and executed evidence | Remaining defects | Responsible builder | Controlled synthetic startup | Production deployment |
|---|---|---|---|---|---|
| #340 `add0da831c101af7061b7cf6309ce8041ed969aa` / `efe0d3be098cb3ef835d5cae24cf9d692954abdd` | **FAIL**, prior independent exact-SHA review; no new tests in this verification. | **P1** retrieved Blueprint source bypasses owner-aware egress policy. | Codex, when available. | Block use of this frozen candidate for any provider-backed Blueprint generation. | **Blocked.** |
| #349 `6598559b138e0b7b4b9af0a2da80d25b1df8d6f8` / `88b83fadd2a25ab6a21ce93ee0ea6bdcc1f580ad` | Security **PASS in isolation** per prior review; migration integration is not approved by that scoped result. | **P2** historical standalone-0069 integration needed separate verification; #363 now passes the historical and populated provenance tests but its full CI remains red. | Codex, when available. | Fresh disposable DB path can be considered only on a candidate with the tested serial migration chain; do not infer acceptance of this frozen SHA. | **Blocked** until the selected migration candidate is fully green and damaged-state handling is resolved where applicable. |
| #351 `a0063d4da6bce871dead53fcae26bee546c92859` / `8687e719f3caf1cd5773c55f84ecaa0d10365711` | **PASS, scoped** for original integrity per prior review; full CI/security acceptance is **NOT VERIFIED**. No new tests here. | No newly confirmed P0–P2 defect in reviewed scope; incomplete CI evidence remains a release gap. | Builder assignment not independently verified. | No block from the scoped finding alone; use only a current integrated candidate and synthetic data. | **Blocked** by incomplete overall acceptance, not by a newly reproduced #351 defect. |
| #352 `07ce4a1d02d0eaa326acb53c0cb1d0a4708ee765` / `cc454df61bcfd0eccaf98ccb0b057fa823acf96c` | **FAIL**, prior integration review; no new tests against this frozen SHA. | **P1** inherits #340 egress bypass; **P3** mutable installer. Its dual-version migration state has no verified recovery. | Codex, when available. | Do not use this frozen integration candidate; a damaged DB matching its dual-version state is separately blocked. | **Blocked.** |
| #358 `c7adaffeee156a0e4373908e7fc6981da3d9759e` / `c4340e60e4cbc7d94425e223c4683d0471e33aad` | **PASS, scoped**; exact-SHA retry-floor, wake, and multi-worker tests: **11 passed**. Backend CI passed in run 42485564040. | **P1 release check:** unallowlisted high/critical npm audit advisories. **P2 release check:** Playwright cannot install Chromium on ubuntu26.04 with pinned Playwright 1.56.1. Neither frontend nor CI files are changed by #358; exact-base run was cancelled, so baseline failure at that exact base is not verified. | Claude owns reliability; dependency/runner remediation stays separate from #358. | These unrelated failures do not block isolated backend behavior testing. | **Blocked** while required checks fail. |
| #360 `3053ce3149207b6cc7476b75b602df643afcfb75` / `82fb013874c5edb30da4f762fdede6e373d10794` | **FAIL**; exact-SHA synthetic run: 5 passed, 2 failed on allowed-receipt durability; separate secret/log check passed. CI runs 42489220833 and 42489176923 passed. | **P2** allowed external dispatch is not durably audited before invocation or after provider error/empty output. Denials are durable; `NEVER_EGRESS` and private-key content do not reach the provider; synthetic credentials/source text were absent from provider payload, ledger, and captured logs after redaction. | Codex, when available. | A local-only controlled test is possible only with external fallback disabled; do not exercise external Blueprint dispatch until fixed. | **Blocked** for any external-provider Blueprint path; global gate remains closed. |
| #363 `1d3fcb0440aca0f460330ecacfb99c15809665fa` / `cc1b68315bc2b6582088c70302f4654e670cb862` | **FAIL / BLOCKED**; disposable PostgreSQL: historical standalone-0069 upgrade **passed**, populated provenance-value downgrade/upgrade **passed**, latest migration round-trip **failed**. CI runs 42489636634 and 42489615897 fail backend/aggregate. | **P2** current CI/test-contract failure at no-op 0073 downgrade (`0073` → `0072` changes revision marker, not schema; generic test asserts schema must change). No data loss was reproduced. Damaged dual-version recovery remains **NOT VERIFIED**. | Codex, when available. | Fresh synthetic DB startup is not contradicted by these three tests, but do not treat the PR as accepted until CI is fixed. Upgrading a DB matching the damaged #352 state is blocked. | **Blocked** pending green backend CI and an explicit recovery decision/test for any affected database. |
| #364 `6c6fa215c37cf444f6c68c4c1ef6ab51c4bfd9fb` / `9777816d69a4b2cc4baf26f42e6055174a4a1200` | Installer integrity **PASS, scoped**; local workflow integrity **4 passed**; CI 42489984734 passed; real-local 42490481184 passed pre/post restart, including Blueprint generation and approval. | **P2 reliability:** run 42490072698 installed Ollama but the real `/api/chat` inference hit its 120s timeout (Ollama log: HTTP 500 after 2m); one later full pass does not yet establish stable latency. **P3 follow-up:** model pulls remain tag-based. | Copilot owns the reviewed pin; Claude owns inference/reliability follow-up. | Installer itself does not block a controlled run; isolate, preload models, and monitor the observed inference timeout. | **Not yet accepted** as stable real-local operation; retain as a production-readiness blocker until repeated representative runs are reliable. |

### Gate decision

The #360 egress authorization checks and denial/redaction behavior are executable and pass
synthetic adversarial tests, but allowed-call receipts fail the required durability test.
The #363 historical and populated provenance paths now have direct disposable-database
evidence, but its latest-revision CI check is still red and the damaged #352 state remains
untested. #358 has a scoped backend PASS, not a green PR. #364 has a scoped installer PASS
and a later successful end-to-end run, but the prior reproducible 120-second chat-inference
timeout means stability is not established. Frozen #340/#352 remain FAIL.

**Overall release state remains FAIL / NOT READY.** A constrained synthetic run can only use
an isolated fresh database, synthetic content, local provider operation, and no external
fallback; this is not startup approval. External-provider Blueprint use and production
deployment remain blocked. `first_cycle_go=false`.

**No merge, deployment, production-data access, or startup authorization.**
`first_cycle_go=false`.
