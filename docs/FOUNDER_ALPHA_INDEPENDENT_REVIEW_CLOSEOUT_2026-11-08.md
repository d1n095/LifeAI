# Founder Alpha Independent Review Closeout

**Review date:** 2026-11-08
**Overall verdict:** **FAIL / NOT READY**
**Startup gate:** `first_cycle_go=false`

This report records exact-SHA evidence for the Founder Alpha remediation candidates and
Claude's WAITING_PROVIDER correction. It is an independent review checkpoint, not formal
security acceptance. No candidate code was changed; no production database, production
secret, merge, deployment, or startup authorization was used.

## Exact-SHA verdicts

| Candidate | Exact head | Verdict and scope | CI evidence |
|---|---|---|---|
| #358 WAITING_PROVIDER | `c7adaffeee156a0e4373908e7fc6981da3d9759e` | **PASS, scoped** for retry floor, zero-base semantics, multi-worker recheck/lease behavior, and regression coverage. Not full release approval. | Backend unit/integration job passed in [run 42485564040](https://github.com/d1n095/LifeAI/actions/runs/42485564040/job/128746544338). Whole PR remains red: npm audit and both Playwright E2E jobs fail. |
| #360 Blueprint egress remediation | `3053ce3149207b6cc7476b75b602df643afcfb75` | **FAIL.** Source gate, owner binding, denial-before-dispatch, redaction, and successful local-provider classification look correct. A P2 audit-integrity issue remains. | CI runs [42489220833](https://github.com/d1n095/LifeAI/actions/runs/42489220833) and [42489176923](https://github.com/d1n095/LifeAI/actions/runs/42489176923) passed. Local focused pytest was unavailable in the reviewer environment. |
| #363 historical migration compatibility | `1d3fcb0440aca0f460330ecacfb99c15809665fa` | **FAIL / BLOCKED.** Historical standalone-0069 upgrade passes; latest migration round-trip fails. Provenance-value round-trip and damaged-state recovery remain unverified. | Backend and required aggregate fail in [42489636634](https://github.com/d1n095/LifeAI/actions/runs/42489636634) and [42489615897](https://github.com/d1n095/LifeAI/actions/runs/42489615897). |
| #364 pinned Ollama installer | `6c6fa215c37cf444f6c68c4c1ef6ab51c4bfd9fb` | **PASS, scoped** for installer artifact integrity. Not full release approval. | Normal CI [42489984734](https://github.com/d1n095/LifeAI/actions/runs/42489984734) and real-local acceptance [42490481184](https://github.com/d1n095/LifeAI/actions/runs/42490481184) passed. Earlier real-local run [42490072698](https://github.com/d1n095/LifeAI/actions/runs/42490072698) failed grounded retrieval after indexing. |

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
- **Evidence level:** Source-traced; the failing-provider case was not executed against the
  exact SHA by this review. The existing
  `test_synthesis_failure_rolls_back_no_partial_draft` exercises a synthetic provider
  failure and verifies no partial Blueprint is saved, but it does not assert that the
  disclosure receipt survives. Existing denial and redaction tests likewise do not cover
  receipt durability after provider failure.
- **Remediation:** Persist a content-free attempt receipt durably before invoking an
  external provider, using a transaction boundary that survives rollback of the
  Blueprint/request transaction. If receipt persistence fails, fail closed and do not call
  the provider. Record the eventual outcome separately without storing raw prompt/source
  content. Avoid committing unrelated request writes as a side effect.

### Prepared synthetic regression

The following pytest test is designed to be added to
`backend/tests/backend/test_founder_alpha_blueprint_e2e.py` on a remediation candidate. It
uses only the existing synthetic provider and database fixtures. It is expected to fail on
#360 until the receipt transaction is made durable; it has **not** been run or certified by
this review.

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
hashes/redaction categories rather than the original key. Extend that test with `caplog`
and serialized-event assertions so neither synthetic credentials nor sensitive source
text appear in logs or audit fields. Add a rollback-after-authorization test proving the
receipt survives an outer request rollback. These tests are separate from implementation
and acceptance must run against the corrected exact SHA.

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
  and hash fields.
- **NOT VERIFIED:** Non-NULL embedding provenance values surviving downgrade/upgrade.
  Recovery of the already-damaged #352 state where the version table contains both `0069`
  and `0073` but `document_chunks` lacks identity columns is also **NOT VERIFIED**. The
  candidate does not demonstrate reconciliation for this state; do not claim automatic
  repair or infer unknown provenance.
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
autonomy soak. Independent scoped verdict: **PASS**; no local tests were rerun because the
reviewer checkout was not at that SHA.

The PR remains CI-red: frontend npm audit reports unallowlisted high/critical advisories;
the full-stack and same-origin Playwright jobs cannot install Chromium with Playwright
1.56.1 on `ubuntu26.04-x64`. Fix these CI blockers independently; do not attribute them to
the backoff change or treat the scoped PASS as a green PR.

### #364 — pinned Ollama installer

Both `founder-alpha-verification.yml` and `real-local-ai-verification.yml` pin the
v0.40.15 Linux amd64 release asset and verify SHA-256
`628c018404a4206493efd38e46251c154ede5141b8b8dc85c659b3b027c47379` before extraction.
The digest matches the GitHub release API asset digest; corrupted-artifact tests confirm
failure before extraction. Exact-SHA normal CI and real-local pre/post-restart acceptance
passed.

The earlier real-local run 42490072698 failed after indexing with
`chat cited uploaded doc=False`; its log does not prove a timeout cause. This remains a
retrieval/reliability follow-up, not evidence that the checksum pin failed. Model pulls
remain registry-tag-based rather than content-digest-pinned; that is outside this
installer review.

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

**No merge, deployment, production-data access, or startup authorization.**
`first_cycle_go=false`.
