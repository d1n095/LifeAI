# Founder Alpha read-only first-start checklist

This checklist prepares a controlled startup; it does not execute one. Founder authorization is mandatory at every marked checkpoint. `first_cycle_go=false` until the security gate matrix is closed.

## 1. Freeze and authority

- [ ] Founder records approved product commit `07ce4a1d02d0eaa326acb53c0cb1d0a4708ee765` and tree `cc454df61bcfd0eccaf98ccb0b057fa823acf96c`.
- [ ] The independent P1 FAIL on #340/#352 source egress is closed on a corrected frozen SHA, with provider-spy evidence proving forbidden retrieved content never reaches an external provider.
- [ ] The historical standalone-`0069` upgrade defect is closed with a data-preserving migration and PostgreSQL round-trip evidence.
- [ ] Independent exact-SHA PASS exists for the remediated full Founder Alpha composition; #352 `07ce4a1...` itself is FAIL / NOT READY.
- [ ] No branch movement, merge, deployment, paid-provider, Recall or unrelated-write authority is inferred from either PASS.
- [ ] Founder explicitly approves the isolated rehearsal scope, owner, repository, worktree, expiry and stop conditions.

Immediate stop: any SHA/tree mismatch, unresolved P0/P1, missing review, unknown required fact or authority wider than the rehearsal.

## 2. Isolated prerequisites

- [ ] Disposable host/network/database/storage are separate from production and real Founder files.
- [ ] No personal OAuth, PAT, host credential helper or production secret is present.
- [ ] Database backup is created and its checksum recorded before migration.
- [ ] Upload storage backup and manifest are created; every content-addressed blob verifies against its path.
- [ ] A restore target exists and is not the live source path.
- [ ] Logs are bounded, access-controlled and contain no secrets or original document bytes.

## 3. Model provisioning

- [ ] Founder chooses explicit local chat and embedding model names.
- [ ] `DEFAULT_LLM_PROVIDER=ollama` and `DEFAULT_EMBEDDING_PROVIDER=ollama`, unless the Founder explicitly approves a different provider.
- [ ] `DEFAULT_LLM_MODEL` and `DEFAULT_EMBEDDING_MODEL` are non-empty and correspond to installed local models.
- [ ] `CHAT_FALLBACK_ORDER` is empty for the first rehearsal.
- [ ] Ollama is reachable only through the intended private topology; no paid key or external egress is available.

Immediate stop: missing model identity, dimension mismatch, unexpected provider selection, paid fallback or undeclared network egress.

## 4. Migration preflight

- [ ] `alembic heads` reports exactly `0073_provider_disclosure_local`.
- [ ] Current database revision and backup identities are recorded.
- [ ] A disposable clone successfully upgrades through historical `0069_provider_disclosure_local_decision`, `0069_embedding_space_identity`, `0070_blueprint_drafts`, `0071_blueprint_approval_guard`, `0072_blueprint_approve_identity`, `0073_provider_disclosure_local`, preserving populated provider-disclosure and embedding provenance.
- [ ] Runtime-role privileges, RLS and SECURITY DEFINER ownership match the independently reviewed contract after upgrade.
- [ ] Downgrade refusal/data-preservation behavior is understood; no destructive downgrade is attempted against real data.

Immediate stop: multiple heads, unknown revision, privilege widening, failed RLS assertion, missing provenance or checksum change.

## 5. Startup diagnostics

- [ ] Only Caddy publishes host ports; backend, frontend, Redis and worker remain private.
- [ ] Backend and worker run as UID 10001; no privileged container or Docker socket is exposed.
- [ ] Redis authentication succeeds only with the configured secret and its data network has no external route.
- [ ] Backend alone runs migrations/privilege boot; worker performs read-only privilege verification.
- [ ] Health endpoints, bounded logs, restart policies and upload-volume ownership are healthy.
- [ ] Founder login succeeds; registration remains disabled; CSRF/session boundaries are active.

Immediate stop: public backend/Redis listener, root application process, failed privilege verification, unexpected migration writer or unhealthy dependency.

## 6. Synthetic document journey

- [ ] Use only synthetic, non-sensitive documents with recorded SHA-256 values.
- [ ] Upload returns an owner-bound job; worker completes exactly once.
- [ ] Stored original bytes retain their SHA-256 and content-address identity.
- [ ] Retrieval cites only the authenticated owner's source.
- [ ] Blueprint v1 is a draft grounded in those sources; regeneration creates the next version without changing v1.
- [ ] Restart backend and worker, then re-read original, chunks, Blueprint versions and audit history.
- [ ] Founder explicitly approves one draft; `approved_by` is the authenticated Founder and direct runtime SQL cannot forge or mutate approval.
- [ ] No retrieval, Blueprint operation or approval authorizes external AI egress.

Immediate stop: cross-owner result, missing citation, changed original hash, partial draft after provider failure, forged identity or contradictory approval.

## 7. Recovery rehearsal

- [ ] Kill the worker during a synthetic job; verify lease expiry/reclaim and exactly one terminal completion.
- [ ] Make the local provider unreachable; verify chat reports failure while preserving the user message and search degrades explicitly to text-only.
- [ ] Restore the disposable backup into a new target; verify archive checksum, blob manifest and byte equality.
- [ ] Attempt the intentionally broken image; verify detection, failed deployment record and automatic rollback to known-good digests with restored traffic.
- [ ] Teardown leaves no orphan containers, networks or volumes.

Immediate stop: lost/duplicated job, silent provider success, missing audit evidence, rollback mismatch or restore divergence.

## 8. Founder decision checkpoint

The Founder receives the exact-SHA gate matrix, independent verdicts, CI/job links, log hashes, known limitations and recovery results. Only the Founder may authorize a subsequent startup action. That authorization does not authorize merge, production deployment, real-file ingestion, paid services, Recall activation or deletion of originals.
