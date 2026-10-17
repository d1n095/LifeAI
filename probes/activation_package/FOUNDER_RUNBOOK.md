# Founder runbook — first supervised MainAI cycle (zero → STOP before merge)

Rules:
- Every step must reach GO before the next starts.
- UNKNOWN = NO-GO.
- Never run a step out of order.
- Never push to a MainAI branch yourself.
- Never merge in this runbook.
- "Recovery" never means retrying a paid step automatically.

Notation:
- `R` = `d1n095/LifeAI`
- `IB` = `mainai/integration-founder-supervised-v1`
- `A` = approved activation SHA

| # | Step | Expected evidence | GO | NO-GO | Recovery |
|---|---|---|---|---|---|
| 1 | **Compose the exact candidate** (ledger §10 order): Path A final, #263, FC-4 successor, activation security, Git transport, `ci.yml` change; Track B excluded | composition manifest (lane → PR → frozen head SHA → examiner PASS evidence SHA); composed SHA `A`; clean tree | every lane has an examiner PASS at the exact head; merges clean apart from the documented additive conflicts | any lane without PASS, any unexplained conflict, Track B present without the D-items | return the lane to its builder; recompose. Never patch during composition. |
| 2 | **Exact-SHA verification of `A`** | rehearsal R-1 MATRIX (0 GAP on required rows); GitHub CI on `A` (from the source branch push): 12 required contexts success, app `github-actions`/15368 | all of it | any failure, skip or missing context | fix in a builder lane; new `A` |
| 3 | **Unpaid rehearsal R-2, R-3, R-4, R-7** (local, no remote mutation) | outputs of each | credential-free worktree; push-grant state machine; CI-wait replay; every tamper → NO-GO | any deviation | builder fix; new `A`; repeat 2–3 |
| 4 | **Configure GitHub: create the App** (`github_app_package.json`), install on only `R`, key in the broker outside the worker | App settings screenshot/JSON; `GET /app/installations/{id}` (JWT) → permissions exactly 5, events none, selection `selected`, repos `[R]` | matches `installation.schema.json` exactly | any extra permission, event or repo; key reachable by the worker | delete the App/installation; recreate. Never "narrow later". |
| 5 | **Apply ruleset phase 1** (`ruleset_phase1.json`, API) | `POST` response: ruleset id; `GET /repos/R/rulesets/{id}` → bypass `[]`, 12 contexts with 15368, `pull_request` params as specified | exact match | API rejects a parameter (e.g. `allowed_merge_methods`) | apply the documented fallback (repo merge methods) and record it; any other mismatch: delete and re-apply |
| 6 | **Create the integration branch** at `A` (Founder admin session) | `GET /repos/R/branches/IB` → `commit.sha == A`; `GET /repos/R/rules/branches/IB` → phase-1 rules; push CI on IB at `A` (after B-07) | sha == `A`; rules effective; CI 12/12 on `A` | sha ≠ `A`; rules missing | delete the branch (allowed before phase 2) and recreate at `A` |
| 7 | **Apply ruleset phase 2** (add `creation`) | `GET` ruleset → `creation` present; effective rules include `creation`, `deletion`, `non_fast_forward`, `pull_request`, `required_status_checks` | exact | anything else | re-apply phase 2 |
| 8 | **Create attestations** (`attestation_schemas/`): repository_identity, activation_sha, required_checks, ruleset_snapshot, no_bypass (incl. runtime R-5 probes), installation, runtime_config → bundle; Founder signs the bundle envelope (`ssh-keygen -Y sign -n mainai-activation-v1`) | JSON files validate against the schemas; `canonical_digest.py` digests; signature verifies against the pinned `allowed_signers` | all validate; bundle `expires_at` ≤ envelope/grant/token expiries | any schema failure, any runtime probe ≠ 403 where 403 is expected, `GET /user` 200 | fix the configuration (step 4/5/7) and regenerate. Never edit a signed payload. |
| 9 | **Unpaid preflight** (R-6) at `A` with the real repo (read-only) | receipt: verdict `go`, `unknown_required_facts=[]`, digests equal to the bundle; receipt age ≤ 15 min | GO | any NO-GO or unknown fact | resolve that fact; regenerate attestations; rerun |
| 10 | **Authorize goal / envelope / spend**: fresh goal; envelope = Option A (one test file) or Option B (two files), capabilities `path_a, read_file, repo_edit, run_tests, push, open_pr`, activation fields = bundle; spend grant `openai/gpt-4o-mini`, `max_requests=5`, `max_cost_usd=0.50`, `per_request=0.10`, `max_prompt_tokens=60000`, `max_completion_tokens=12000`, expiry = session; lesson query recorded | DB rows; envelope `approved_base_sha == A`; grant bound to the envelope; config `CHAT_FALLBACK_ORDER=openai`; `GITHUB_WRITE_ENABLED=true` set now | exact values | anything wider | supersede the envelope / revoke the grant; recreate |
| 11 | **Plan** (one paid call) and inspect | plan: exactly one `repo_edit` with created-event `target_files` == the envelope files (Option A: 1 file), `verify_only_tests` empty, `target_source=planner`; one `open_pr`; optional `read_only_audit` | exactly that | other task types, other targets, `agent_assignment` | reject (cancel the goal); new goal (within the grant) or stop |
| 12 | **Approve repo edit**; after it completes, **inspect the local commit** | worktree `HEAD`; `git diff --name-only --no-renames A..HEAD` == the target files; diff readable and minimal; targeted test PASS checkpoint; `.git/config` has no credential; spend ledger rows | all | an extra file, a credential, test fail, spend > ceilings | cancel the task (worktree discarded); no push. New cycle if desired. |
| 13 | **Issue one-shot exact-SHA push grant** (`POST /tasks/{id}/push-grants` with `local_commit_sha`, `A`, IB, preflight receipt id, short expiry) | grant row `active`, binding exact | grant created; receipt still live | receipt stale, any binding mismatch | regenerate the receipt (step 9); never reuse a grant |
| 14 | **Verify the push / remote SHA** | grant `consumed` exactly once; `GET /repos/R/branches/claude%2Fmainai-task-<id>` sha == local SHA; pusher = `<slug>[bot]` | equal | mismatch, or a second push attempt | stop. Crash after consumption: inspect remote; if equal, record; else a new Founder grant. |
| 15 | **Approve open_pr separately** | draft PR: `user.login == <slug>[bot]`, `base.ref == IB`, `head.sha ==` approved SHA, `draft == true` | all | any mismatch | close the PR; stop |
| 16 | **Wait for required CI** on the head SHA | the 12 contexts success from app 15368 (push runs, and PR runs once T-2 lands); Vercel ignored | 12/12 | any failure, inherited or not: no paid replan | stop; a new goal requires new authorization |
| 17 | **Independent review → STOP** | examiner verdict bound to repo + PR number + head SHA (exam evidence branch SHA); ruleset re-snapshot digest == attested; spend ≤ grant | PASS and digest unchanged | RETURN_TO_BUILDER, or digest changed | leave the PR open as a draft; record. **Do not merge.** Any merge is a separate Founder decision under the ruleset. |

After step 17: set `GITHUB_WRITE_ENABLED=false`; revoke the spend grant; delete or expire the installation tokens; keep all evidence.
