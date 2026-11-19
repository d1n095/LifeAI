# Founder release candidate — private use, verified

Branch: `freebuff/founder-alpha-ops-recovery-20261118` · Draft PR **#461** · `first_cycle_go`
remains **false**. Second pass (2026-11-19): R12 re-probed and still absent, mobile console
covered, Blueprint fencing defect found and fixed, architecture contract added as gate item 15. This document is committed in the release-candidate commit; `git log -1` on
the branch names it exactly.

## Verdict

| Question | Answer | Basis |
|---|---|---|
| Usable privately by the founder today, without the Cognitive Core? | **GO** | 14/15 release items verified with local-real evidence |
| Full integrated release including Z.ai Round 12? | **NO-GO** | item 4 is `external_blocked`: the artifact does not exist on this host |
| Merge / deploy / change the default branch? | **Not authorised** | founder decision; nothing merged or deployed |
| Enable autonomous privileged actions? | **No** | approvals stay enforced; `first_cycle_go` unchanged |

`backend/scripts/acceptance/founder_release_acceptance.py` exits **2** here, not 0 — by design:
"nothing failed" and "everything was verified" are different answers. 0 means every item
executed and passed.

## Reproduce it

```bash
# services (this host): PostgreSQL 5433, Redis 6379, Ollama 11434
export DATABASE_URL=... APP_DATABASE_URL=... STORAGE_ROOT=... REDIS_URL=... \
       SECRET_KEY=... FOUNDER_EMAIL=... FOUNDER_PASSWORD=... OLLAMA_BASE_URL=...
export MAINAI_BACKUP_KEY_FILE=/path/to/32-byte-key

cd backend
python scripts/setup/bootstrap_database.py            # idempotent install: role, migrations, grants
python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
python -m app.worker                                   # exactly one worker; it shares STORAGE_ROOT

cd ../frontend && INTERNAL_API_URL=http://127.0.0.1:8010 npx next start -p 3020

cd ../backend
python scripts/acceptance/founder_release_acceptance.py --out release-acceptance.json
```

Use the project interpreter, with its `bin` on `PATH`: one cancellation test spawns a real
`python` subprocess, and a `PATH` that cannot resolve it fails that test for an environmental
reason. Measured, not assumed — the first run of this pass failed item 10 exactly that way.

`bootstrap_database.py --verify-only` mutates nothing and is the readiness check to run before
starting the API. It must pass on a database that is already installed — that property is now
tested (see defect 1).

## Release gate — 15 items

| # | Item | Evidence | Result |
|---|---|---|---|
| 1 | Fresh installation from an empty database | local_real | passed — scratch DB created, bootstrapped, verified twice, dropped |
| 2 | Real founder login through the same-origin proxy | local_real | passed |
| 3 | A real local model answers a real question | local_real | passed (`ollama/qwen2.5:0.5b-instruct`) |
| 4 | Cognitive reasoning and premise challenge | external_blocked | **blocked — Z.ai R12 absent** |
| 5 | Evidence provenance and citations | local_real | passed (retrieved sources with scores) |
| 6 | Cross-session memory and decision continuity | local_real | passed |
| 7 | Safe agent execution | local_real | passed |
| 8 | Approval and denial enforcement | local_real | passed |
| 9 | Prompt-injection resistance (one canonical fence) | local_real | passed (7 tests) |
| 10 | Worker cancellation and recovery | local_real | passed (11 tests) |
| 11 | Real browser functionality, desktop and mobile | local_real | passed (Chromium, 9 specs incl. the 390×844 console spec) |
| 12 | Encrypted backup and clean restore that boots | local_real | passed (push→verify→restore→boot) |
| 13 | Model-provider switching and fail-closed fallback | local_real | passed (18 tests) |
| 14 | Security and regression suites | local_real | passed |
| 15 | Startup architecture contract | local_real | passed (7 checks; see `docs/ARCHITECTURE_STARTUP_CONTRACT.md`) |

Suite totals in the recorded run: 87 backend tests (auth 28, RLS 18, injection 7, cancellation
11, providers 18, memory 5), 9 browser specs (desktop + mobile), 7 architecture checks, plus the
structural install checks, the 12-step founder workflow, the fresh install and the backup
roundtrip. Reports: `docs/evidence/release-acceptance.json`,
`docs/evidence/founder-alpha-acceptance.json`, `docs/evidence/founder-workflow.json`,
`docs/evidence/architecture-contract.json`. The mobile console screenshot is
`docs/evidence/founder-console-mobile.png`, captured by the spec itself.

CI on the parent commit `d029298`: run `44502219106` **success**, Security `44502219193`
**success**. That is CI evidence, not a substitute for the local run above.

The `git_commit` field inside `docs/evidence/release-acceptance.json` names the tree commit at
_generation_ time (`b2e9ea6`). That tree was then amended to add the report itself and to
regenerate the policy lock, so the release-candidate commit has a different id with identical
code. Nothing about the measured result depends on the id; re-running the gate prints its own.

## Defects found and fixed in this pass

1. **The readiness check contradicted itself.** `bootstrap_database.py` verified grants before
   it enforced the boot contract, so a fresh install passed and every later `--verify-only` on
   the same healthy database failed (`mainai_job_events` "lacks UPDATE/DELETE" — privileges
   `app/rls.py` deliberately revokes for the append-only event log). Fixed by enforcing the boot
   contract before verifying grants and by asserting the append-only table's exact allowed set
   (`SELECT, INSERT`) and rejecting anything wider. Regression test:
   `tests/backend/test_database_bootstrap.py`.
2. **Running the test suite could delete the founder's database.** The session fixture DROPs the
   database `DATABASE_URL` names. A release-gate run exported the installed database into the
   pytest environment, so teardown dropped it. Fixed twice over: `conftest.py` now refuses any
   name that is not `lifeos_test*` (`pytest.UsageError` before the first DROP, tested in
   `tests/backend/test_pytest_database_drop.py`), and the release gate rewrites both URLs to a
   `lifeos_test_gate_*` database with the same credentials. The suite also no longer inherits the
   app's provider and cookie configuration, which had made 4 tests fail for reasons unrelated to
   the code under test.
3. **The Blueprint prompt path fenced raw text.** `blueprint.py` imported the shared markers and
   appended each retrieved excerpt between them without defusing the text first. A document that
   contained `<<< END UNTRUSTED SOURCE EXCERPT <<<` could therefore close its own fence and
   continue in operator voice inside the synthesis prompt — while every existing assertion still
   passed, because the marker *count* was correct. The chat path already used the defusing
   function; this path did not, which is exactly the "two builders fencing differently" failure
   the fence module warns about. Fixed by routing both the source excerpts and the continuity
   block through `fence_untrusted()`. Regression test:
   `tests/backend/test_founder_alpha_blueprint_e2e.py::test_blueprint_source_cannot_forge_its_own_fence_marker`
   (forged marker becomes `[redacted marker]`; the injected sentence stays inside the fence). The
   new architecture check `single_prompt_fence` is what surfaced it — a count-based test could
   not.
4. **The console made two claims it could not support.** "Senast kontrollerad nu" was a constant
   string while the console polls every 10s and pauses on hidden tabs; it now shows the real last
   check time. A failed execution-goals read rendered "Inga åtgärder väntar på godkännande"; it
   now reads "Kön kunde inte läsas — status är okänd, inte tom."

**Recovery of defect 2 was exercised for real, not described.** The encrypted artifact from
`20261119T112647Z` (taken 90 seconds before the loss) was restored into an empty database,
verified (`verification_errors: []`, revision `0079`), the data confirmed (17 documents, 14
conversations, 28 messages, 1 founder), the database swapped into place by rename, and the API
and worker restarted: `/api/health` returned `status: ok`, `worker: alive`, `storage.writable:
true`, model reachable, no missing models. That is the disaster-recovery path executing end to
end.

## Migrations and rollback

- Single head: **`0079`** (`0079_audit_log_append_only.py`). `0075`–`0078` are applied and
  reviewed; see `docs/ZAI_ROUND12_INTEGRATION_STATUS.md` for the R12 numbering collision.
- No migration is added by this change set, so rollback is not a schema downgrade.
- Application rollback: redeploy the previous commit (`d029298` is the reviewed parent).
- Data rollback: restore the newest encrypted artifact —
  `python backend/scripts/ops/offsite_backup.py restore --destination <dest> --name <artifact> --target-url <fresh-db> --confirm --key-file <key>`,
  then `bootstrap_database.py --verify-only` against it before pointing the app at it. The tool
  refuses to restore over the database the backup came from; that guard is intentional.

## Security assessment

- One canonical prompt fence (`app/rag/prompt_fencing.py`), policy-locked, 7 adversarial tests,
  and both prompt builders (chat and Blueprint) now wrap content through the same defusing
  function. The property is checked structurally by release item 15, so a future Core import
  cannot add a second fence unnoticed.
- Approvals are enforced in the backend; the console can only request approve/reject, and a
  rejected task is not executed. Cancellation is cooperative and observable.
- Append-only job events and audit log: `mainai_app` holds `SELECT, INSERT` only on the event
  log; `audit_logs` never grants `DELETE`. Both are asserted at install and at every boot.
- Backups are AES-256-GCM with per-chunk nonces, authenticated headers, checksums at each hop.
- Secret scanning and push protection are **enabled**; Dependabot security updates are
  **disabled**; CodeQL needs GHAS + `CODEQL_ENABLED`.
- Branch protection is **not active**. `Security` and `All required checks passed` are verified
  real contexts on run `44502219106`, but nothing requires them yet:
  `python backend/scripts/ops/verify_release_governance.py` reports `ready_to_apply: False`,
  blocking only `branch.main` and `protection.main`. No repository setting was changed.

## Outstanding blockers and founder decisions

1. **Z.ai Round 12** — the exact missing transfer step is delivery of `cc4d2804` to this host,
   as any one of: (a) a git bundle plus its SHA-256 and `git bundle verify` output, (b) a
   `git format-patch` series whose cover letter names the produced SHAs, or (c) a pushed scratch
   ref carrying that commit. Nothing else is missing on this side. Details and the accepted
   migration strategy (merge revision on `0079`, never renumbering applied migrations):
   `docs/ZAI_ROUND12_INTEGRATION_STATUS.md`.
2. **Protected release branch** — create `main` at the reviewed baseline, protect it, then
   switch the default. Exact commands are printed by the governance verifier; they are not run.
3. **Genuinely off-host backup** — the roundtrip used a controlled remote-equivalent
   (`file://`). Manifests still say `off_host_verified: false`, because a copy on the same host
   is not disaster recovery. Needs an authorised destination and key custody.
4. **The founder's real corpus** — the measured corpus is synthetic/test documents. Retrieval
   quality must be re-measured on real material before `first_cycle_go` is considered.

## Not verified (stated plainly)

- Cognitive reasoning and premise challenge: not present in this tree, therefore not tested.
- Off-host retention: not tested; no authorised remote exists.
- Branch protection enforcement: not active, therefore not tested.
- Visual review by a human: the mobile and desktop screenshots are captured by the specs, and the
  layout assertions are automated (no horizontal overflow at 390 px, nav reachable, composer
  usable). Nobody has looked at the screenshots and judged them pleasant.
- The founder's real corpus: the measured corpus is synthetic/test documents, so retrieval
  quality on real material is still unmeasured.
- Requirements that are architecture contracts, not features: the personal user-AI, research
  source adapters, per-user at-rest encryption and the Life products are **not built**. Their
  seams are checked, nothing more — see `docs/ARCHITECTURE_STARTUP_CONTRACT.md`.
