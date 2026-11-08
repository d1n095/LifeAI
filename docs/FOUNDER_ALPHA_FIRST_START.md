# Founder Alpha: review closure and controlled first start

This is an operational handoff for the frozen integration candidate in PR #352, exact commit
`07ce4a1d02d0eaa326acb53c0cb1d0a4708ee765`, tree
`cc454df61bcfd0eccaf98ccb0b057fa823acf96c`. It does not authorize a merge, deployment,
production access, Recall activation, autonomous execution, or import of real files.
`first_cycle_go=false` remains authoritative.

## Evidence and dependency closure

| Candidate | Exact SHA | Dependency in #352 | Gate before controlled startup |
|---|---|---|---|
| #349 | `6598559b138e0b7b4b9af0a2da80d25b1df8d6f8` | Embedding-space identity and downgrade provenance | Independent PASS on this exact SHA |
| #340 | `add0da831c101af7061b7cf6309ce8041ed969aa` | Blueprint grounding, immutable versions and approval | Independent PASS on this exact SHA |
| #351 | `a0063d4da6bce871dead53fcae26bee546c92859` | Content-addressed, integrity-checked original retrieval | Independent PASS on this exact SHA |
| #352 | `07ce4a1d02d0eaa326acb53c0cb1d0a4708ee765` | Exact composed candidate | Independent composition PASS plus all required exact-SHA checks |

#352 has builder evidence, not certification: mandatory CI run `42426029333`, PR run
`42426032374`, real-local acceptance run `42426029291`, backend `2373 passed, 1 skipped`, and
real-local `13/13 PASS`. Copilot or another reviewer who did not implement the relevant code owns
independent examination. Claude owns infrastructure/runtime-reliability work. Codex must only
prepare a new child remediation branch after a concrete review finding. The frozen SHAs above
must never be amended to close a finding.

The machine-readable contract is `docs/founder_alpha_first_start_manifest.json`. Run:

```bash
python backend/scripts/ci/verify_founder_alpha_first_start.py
```

This verifies frozen ancestry, tree identity and one migration head. It intentionally reports
`NO-GO` and exits non-zero while reviews or configuration are unknown. It never converts builder
evidence into a review PASS.

## Migration 0073 ancestry

The source branches reused the numeric 0069 slot for different revisions. #352 resolves the
collision by serializing both capabilities into one lineage:

```text
0068
  -> 0069_embedding_space_identity
  -> 0070_blueprint_drafts
  -> 0071_blueprint_approval_guard
  -> 0072_blueprint_approve_identity
  -> 0073_provider_disclosure_local
```

This is not an Alembic merge-head: each revision has exactly one parent and the repository has
exactly one head. `0069_embedding_space_identity` adds nullable identity and preserves known
model/dimension provenance in its migration-owned backup table across an intentional downgrade.
`0070` creates the owner-scoped Blueprint table; `0071` adds immutable approval; `0072` closes
runtime-role GUC spoofing; `0073` adds the local-provider disclosure decision. Their order is
security-significant and must not be renumbered or independently replayed.

Before any startup, require all of the following on the exact approved image/source:

```bash
cd backend
python -m alembic heads        # exactly: 0073_provider_disclosure_local (head)
python -m alembic current      # after upgrade: exactly the same revision
```

Never downgrade a database containing real Founder data as a rollback shortcut. Roll back the
application image only when `scripts/vps/rollback.sh` confirms the prior image understands the
current schema. Otherwise stop and obtain a DBA-reviewed recovery plan.

## Phase A: isolated synthetic rehearsal

This phase is allowed only after the Founder authorizes the rehearsal itself. It uses a disposable
Postgres database, Redis instance, storage directory and synthetic documents. It does not use
production secrets or real project files.

1. Check out the exact independently accepted candidate. Verify commit and tree:

   ```bash
   test "$(git rev-parse HEAD)" = "07ce4a1d02d0eaa326acb53c0cb1d0a4708ee765"
   test "$(git rev-parse HEAD^{tree})" = "cc454df61bcfd0eccaf98ccb0b057fa823acf96c"
   ```

2. Create an env file outside the repository. Use unique synthetic credentials and local-only
   URLs. Required safety settings are:

   ```dotenv
   ENVIRONMENT=development
   DATABASE_URL=postgresql://postgres:<synthetic>@127.0.0.1:5432/lifeai_founder_alpha_rehearsal
   APP_DATABASE_URL=postgresql://mainai_app:<synthetic>@127.0.0.1:5432/lifeai_founder_alpha_rehearsal
   REDIS_URL=redis://127.0.0.1:6379/15
   SECRET_KEY=<unique synthetic value>
   FOUNDER_EMAIL=founder-rehearsal@lifeai.invalid
   FOUNDER_PASSWORD=<unique synthetic value>
   FRONTEND_ORIGINS=http://127.0.0.1:3020
   PUBLIC_APP_URL=http://127.0.0.1:3020
   COOKIE_SECURE=false
   ENABLE_SCHEDULED_CLEANUP=false
   STORAGE_ROOT=/tmp/lifeai-founder-alpha-rehearsal/uploads
   DEFAULT_LLM_PROVIDER=ollama
   DEFAULT_LLM_MODEL=qwen2.5:0.5b
   DEFAULT_EMBEDDING_PROVIDER=ollama
   DEFAULT_EMBEDDING_MODEL=all-minilm
   OLLAMA_BASE_URL=http://127.0.0.1:11434
   CHAT_FALLBACK_ORDER=
   OPENAI_API_KEY=
   ANTHROPIC_API_KEY=
   GOOGLE_API_KEY=
   DEEPSEEK_API_KEY=
   OPENROUTER_API_KEY=
   GITHUB_WRITE_ENABLED=false
   GITHUB_AUTO_MERGE_ENABLED=false
   FIRST_START_ISOLATED_REHEARSAL=true
   FIRST_START_SYNTHETIC_DATA_ONLY=true
   ```

3. Run the read-only preflight. It prints no secret values:

   ```bash
   python backend/scripts/ci/verify_founder_alpha_first_start.py \
     --env-file /absolute/path/to/rehearsal.env
   ```

   A repository/configuration failure is actionable. An unresolved review remains an
   `unknown_required_fact` and keeps the overall result `NO-GO`; do not edit the manifest to
   manufacture a GO.

4. Reproduce the already-defined exact journey rather than inventing a new harness. Run the
   `Founder Alpha — real local AI` workflow against an immutable SHA, or mirror its service and
   command order in isolated infrastructure: pgvector Postgres, restricted `mainai_app`,
   migrations, runtime privilege policy, Ollama, backend, worker, then frontend. Never point the
   rehearsal at Supabase, the VPS upload volume, production Redis, or production Ollama state.

5. Use a synthetic text/DOCX file with a locally recorded SHA-256. Confirm:
   Founder login; upload accepted; worker reaches a terminal success state; downloaded bytes and
   SHA match; extracted source is owner-scoped; Blueprint v1 and v2 cite only that source; neither
   version is approved automatically; explicit Founder approval succeeds; an ungrounded request
   fails visibly; backend and worker restart; the conversation, source identity, original bytes,
   versions and approval survive unchanged.

6. Stop all rehearsal processes, archive redacted logs plus the exact SHA/test report, and delete
   the disposable database/storage only after evidence capture. Never copy synthetic credentials
   into the future production secret store.

## Phase B: prerequisites for real authorized files

Every item is mandatory. Unknown means stop.

- Independent PASS exists for #349, #340, #351 and the exact composed candidate.
- Founder has selected the exact reviewed commit and separately authorized merge/start.
- Claude's infrastructure/runtime review is complete for the selected topology.
- Backend and frontend images are built from that exact commit and pinned by registry digest.
- The deployment record binds source SHA, both image digests, Compose checksum and Caddyfile
  checksum; no mutable image tag or branch name is accepted.
- A tested database backup/restore point exists. The `lifeai_uploads` backup/restore drill has
  verified its manifest and content-addressed blobs. Secrets are recoverable from an independent
  secret store; they are intentionally absent from application backups.
- Postgres uses separate migration/admin and restricted runtime roles; migrations and
  `apply_runtime_privileges.py` have completed before requests are accepted.
- Local Ollama chat and embedding models are pinned and verified. The embedding model/dimension
  matches the active embedding-space identity. All paid-provider keys are absent and fallback is
  empty unless the Founder separately authorizes a later provider change.
- The shared backend/worker upload volume is writable only by their non-root UID and has enough
  capacity for originals plus backup retention.
- HTTPS, host-only secure cookies, SMTP, Redis authentication, rate limiting and log rotation have
  been verified without printing secrets.
- GitHub write and auto-merge remain disabled. No merge, deployment, external egress, Recall or
  autonomous capability is inferred from a successful startup or review.
- Founder has selected a small initial file set, confirmed ownership/legal authority, recorded
  filenames and SHA-256 values locally, and accepted format limits. Scanned PDFs/images require
  OCR that Founder Alpha does not yet provide; do not describe an unread scan as ingested.

## Controlled-start acceptance checklist

### Before start

- [ ] Exact SHA/tree and all independent review receipts match the manifest.
- [ ] Exact-SHA CI and real-local acceptance are green; no moving branch is evidence.
- [ ] One Alembic head exists and backup/restore drill is current.
- [ ] Digest-pinned images, Compose and Caddy checksums are recorded.
- [ ] Secrets file is root-only, unique keys are present once, and no values appear in logs.
- [ ] Founder has approved the synthetic rehearsal or real-file scope as a separate action.

### During start

- [ ] Only backend performs migrations/privilege mutation; worker performs read-only verification.
- [ ] Backend, worker, frontend, Redis/Ollama and Caddy become healthy within bounded time.
- [ ] Logs show no restart loop, migration mismatch, provider fallback or storage permission error.
- [ ] Runtime database session is `mainai_app`; Founder approval uses only its reviewed privileged
  path and cannot be produced by runtime-role SQL.
- [ ] Import status, original SHA, source owner, citations and Blueprint version/approval are
  recorded without logging document content or credentials.

### Recovery and rollback

- [ ] On any integrity, authorization, migration, model-space or storage discrepancy: stop new
  imports immediately, preserve evidence and do not approve the resulting Blueprint.
- [ ] `deploy.sh --confirm` health failure invokes the existing bounded rollback. Verify the
  deployment record and health after rollback; rollback success is observed state, not assumed.
- [ ] Do not downgrade the database or overwrite originals. Restore originals only from a verified
  archive into isolated volumes first; compare manifests and SHA-256 before any live restoration.
- [ ] If database and upload-volume snapshots are not from a consistent checkpoint, stop and
  reconcile rather than attaching metadata to unverified bytes.
- [ ] A failed local model call remains failed/visible. It must not trigger paid fallback,
  automatic replanning, approval or external egress.

### Authorization boundaries

- [ ] Upload permission does not imply Blueprint approval.
- [ ] Retrieval permission does not imply external AI egress.
- [ ] Test PASS does not imply independent certification.
- [ ] Certification does not imply merge, deployment, real-file import, Recall or autonomy.
- [ ] Founder silence never grants authority; each later transition needs its own explicit gate.

## Remaining owner matrix

| Blocker/action | Owner | Evidence needed |
|---|---|---|
| #349/#340/#351/#352 exact-SHA examination | Independent reviewer, not the implementer | Published PASS/FAIL with exact SHA and reproducible probes |
| Infrastructure and runtime reliability | Claude | Frozen topology/digest review, recovery exercise and runtime evidence |
| Remediation of a concrete finding | Assigned builder on a new child branch | New SHA; original frozen evidence retained |
| Merge, infrastructure choice, secrets and first start | Founder | Separate explicit authorization for each transition |
| Real project-file scope | Founder | Explicit selected files, ownership basis and local hash inventory |

There is no operational shortcut around these gates. Green synthetic evidence makes a later
Founder decision informed; it does not make the decision itself.
