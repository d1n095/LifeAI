# Import Security Candidate Validation Matrix

This matrix separates test results from security acceptance. Frozen candidates remain
unchanged; local tests and reviewer comments do not replace exact-head CI or deployment
equivalent fault injection.

## Dependency and evidence

| Candidate | Exact head | Dependency | Local tests | Independent review | Remote CI | Deployment-equivalent fault injection |
|---|---|---|---|---|---|---|
| #392 parser isolation | `92d4ad3d76634473d1dc4a7a85913ff676090947` | Approved precomposition `7271f72d20450ec6c1db41b2160d13eebac13e39` | Not re-run in this session | Reviewer comment recorded at exact head; not an approval | Run `43570228962` passed at the exact head | Not verified |
| #402 import lease-loss fencing | `5b66814b35a4889c68acab9ed9e0ed3edd9dfbf5` | #392 exact head | Not re-run in this session | Reviewer comment recorded at exact head; not an approval | Run `43611782805` passed at the exact head | Not verified |
| #413 lease/write fencing and parser stderr bounds | `23022662a78abb141792ea7439b715d670cdc640` | #402 exact head | Its worker/import/parser code was exercised locally in #420's test run | Earlier adversarial review found no concrete issue; the later procfs boundary reproducer is a separate residual gap, not security acceptance | Run `43700873775` is `action_required`, zero jobs | Not verified |
| #415 workflow permission hardening | `8538f0f362f062f917debcdaab38935f13fc99cc` | #413 exact head | Static workflow checks and independent workflow review recorded in its history | No significant workflow issue found at reviewed code SHA `0abff90a3069ca142a4f4f9a7257b36e483facb9`; not security acceptance | Run `43704183132` is `action_required`, zero jobs | Not applicable |
| #420 test corrections | `31dd0cfe0a6127e21ea6bb9720f845da6cde155d` | #413 exact head; #415 is a separate sibling PR | 58 worker/import/parser tests and 9 Redis lock/fail-closed tests passed against disposable PostgreSQL/Redis | No significant test-correction issue found; not security acceptance | No backend Actions run; only Vercel checks | Not verified |
| #421 exact-SHA validation workflow | `f1de4ad417d1ac4c1252a36030c9d9ee0184cf3f` | #420 exact head | Workflow definition recognized; target test code already passed locally as above | Not independently reviewed | Run `43713479509` is `action_required`, zero jobs | Not applicable |
| #422 parser procfs boundary reproducer | Test SHA `d2f79d3132f9a9003557f2fb3199550c26987b50` | #420 exact head | The test is an expected failure; `--runxfail` reproduced `visible` instead of `denied` using only a synthetic canary | Not yet reviewed | No backend Actions run; only Vercel checks | Demonstrates same-UID procfs access locally; production deployment replication not run |
| #425 parser isolated-sidecar follow-up | Current tested implementation SHA `2e3cdbce4823f00027467ec2a82da283a9ede71f` (draft); UID/socket hardening remains at `c3f143a55767d7ebe1357548afc6899f375effad` | #422 at `a4f6819fff04d6919d4b46b1c018ba38aad1ab88` | Parser API/isolation: 12 passed, 1 inherited strict xfail. Opt-in Docker boundary: 1 passed, including 413 for oversized input and automatic restart after parser-process crash. Focused transient-outage/archive tests: 4 passed. Duplicate-import idempotency and archive-recovery tests: 2 passed through the rebuilt VPS Compose sidecar socket. | Independent code review of the UID/socket diff through `c3f143a55767d7ebe1357548afc6899f375effad` found no actionable issue; review did not execute containers and did not cover the later retry fix. Not security acceptance. | No backend Actions run; current remote checks are Vercel only | Started the actual parser service from both local and VPS Compose manifests, using a local candidate image and synthetic, non-secret VPS interpolation values; did not start the full application stack. Verified effective parser UID/GID, secretless env, separate PID namespace, no egress, read-only root, single socket mount, shared-socket owner/group/modes, configured CPU/memory/PID limits, real extraction, fail-closed behavior during a real service stop, restart/rebind and post-restart extraction. The parser returned 413 for an oversized request and remained usable. No deliberate OOM/CPU exhaustion or full host/deployment fault injection was performed. |

Dependency order is #392 → #402 → #413. PRs #415 and #420 are separate children of
#413; #421 and #422 are evidence-only children of #420. In particular, #420 does not
contain #415.

## #416 integration failures and #413 coordination

PR #416's full backend job failed twice on exact head
`fb3f3abd3553ce636a37a817e4bcf205b79eb347` (run `43704034822`), each with 2 failed,
2,422 passed, and 1 skipped. Both failures are test defects in #413's lineage, not
production-code failures:

- `test_commit_fence_checks_expiry_after_waiting_for_job_row_lock` calls `SessionLocal()`
  from its thread without importing it. The resulting `NameError` occurs before
  `commit_started` is set, so the parent test times out waiting for the event.
- `test_archive_progress_is_fenced_and_restart_finishes_remaining_files` requires
  `file_results == []`, but the nullable column remains SQL `NULL` when no file progress
  has been committed.

Both corrections already exist in downstream #420 at
`31dd0cfe0a6127e21ea6bb9720f845da6cde155d`: import `SessionLocal` and assert the
empty/falsy state. The two targeted tests passed locally (2 passed) on that descendant.
These are test-only corrections; passing them does not establish that the underlying
lease-fencing implementation is correct.
The diagnosis and recommendation to carry only those test corrections into #413's next
authorized update were posted to #413:
https://github.com/d1n095/LifeAI/pull/413#issuecomment-6897386451. The #413 exact-SHA
full-backend CI remains blocked independently by GitHub's authorization gate below.

## Parser outage retry gap and remediation

Compose-level tests showed the sidecar fails closed when stopped and recovers after restart,
but source inspection found that `httpx` transport failures were wrapped as generic
`ExtractionIsolationError`. The import layer treated that as a terminal per-file extraction
failure, so a parser restart during import could leave a file failed instead of reaching the
worker's bounded retry/backoff path. Follow-up SHA
`2e3cdbce4823f00027467ec2a82da283a9ede71f` adds `ParserServiceUnavailableError`
(an extraction error and transient `ConnectionError`) and propagates only that exception
through both first-import and incomplete-document resume paths. Content/format failures
remain terminal, and production does not fall back to local parsing. Regression evidence:
the configured missing-socket test and archive retry/recovery/idempotency tests pass; the
archive test verifies two files are indexed once when the second-file parse fails once and
then recovers. This is local test evidence, not proof of lease fencing.

## CI authorization blocker

The scoped workflow in #421 is push-only, grants only `contents: read`, disables checkout
credential persistence, references no repository secrets, performs no deployment, and
checks out #420 by its fixed SHA. GitHub recognized the workflow and created push run
`43713479509` for `f1de4ad417d1ac4c1252a36030c9d9ee0184cf3f`, but returned
`action_required` before creating any jobs. The repository Actions-settings API returns
403 to this installation, so the exact approval policy is not visible here.

One repository administrator action is required: open **Actions → Import security
regression tests (frozen SHA) → run 43713479509**. If GitHub presents **Approve and run**,
approve this restricted run. If it does not, inspect **Settings → Actions → General** for
the repository's workflow approval policy and use the supported administrative execution
path. Do not reroute the workflow through a more privileged event or bypass branch/review
protections. No further API approval attempts are planned.

## Parser containment finding and current boundary

The current parser subprocess drops inherited environment variables and applies input,
output, CPU, address-space, file-descriptor, file-size, and wall-clock bounds. However, it
runs under the worker's UID and PID/mount/network namespaces. An adversarial child with
arbitrary code execution can read a same-UID parent's `/proc/<pid>/environ`; test
`test_parser_child_cannot_read_parent_environment_through_procfs` reproduces this using
only `PARSER_PARENT_CANARY=synthetic-value`. This is a demonstrated containment gap, not
evidence that a supported document currently achieves code execution.

The active follow-up branch implements a dedicated parser container rather than granting
`CAP_SETUID` or `CAP_SYS_ADMIN` to the backend/worker. It uses the backend image and parser
libraries, but receives no app env file, database/Redis credentials, upload-volume mount, or
network access (`network_mode: none`). A bounded, UID-restricted Unix socket is the only
shared resource. The parser has separate container PID/mount namespaces, read-only root,
no capabilities, `no-new-privileges`, bounded tmpfs, CPU/memory/PID limits, and a strict
size/time-limited byte-in/text-out protocol. The caller fails closed on timeout, malformed
output, or service absence; production without `PARSER_SOCKET_PATH` fails closed.

The earlier parser/import/recovery suite passed 50 tests with one inherited strict expected
failure for the old same-UID local-process boundary. UID/socket hardening at
`c3f143a55767d7ebe1357548afc6899f375effad` gives the parser UID 10002 and retains group
10001 solely for the caller's Unix-socket access. The directory is mode 0710 and the socket
0660, owned by the parser UID and shared group. The Compose CLI was installed from the
official checksum-verified release for this validation. Both actual Compose parser services
(local and VPS) were started with the candidate image; the full stack was not started. VPS
interpolation used synthetic values and `/dev/null` for its otherwise secret-bearing env
file. Runtime checks verified caller access, PID/environment isolation, no egress,
read-only root, no application mounts, configured resource limits, real extraction, and
413 rejection of oversized input. Unexpected parser-process death incremented the Compose
restart count and extraction recovered after socket rebind. A real Compose stop caused the
production caller to fail closed without local parsing; starting the service restored
extraction. The opt-in Docker test repeats these boundary and restart assertions. These
results do not include deliberate OOM/CPU exhaustion or full host/deployment fault injection.

The independent review of the UID/socket diff found no actionable code-level issue, but
did not run the containers; the later retry-path change has not had a separate independent
review. The updated parser/import/recovery tests pass 12 cases with one inherited strict
xfail. Remote backend CI and formal security acceptance remain pending. Render's combined
container has no sidecar and document parsing/imports remain unavailable there until an
equivalent isolated service exists. This is not security acceptance.
