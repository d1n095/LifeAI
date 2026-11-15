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
| #425 parser isolated-sidecar follow-up | Tested application implementation SHA `f2cede7c8dfaff21fb24e99cbe2addf96f298a2e`; prior evidence/registry SHA `af3e6bf50651c3c943372cbeedbb7bc98736ddf0`; UID/socket hardening `c3f143a55767d7ebe1357548afc6899f375effad` | #422 at `a4f6819fff04d6919d4b46b1c018ba38aad1ab88` | Earlier parser/import/worker/lease regressions: 64 passed, 1 inherited strict xfail. New PostgreSQL/Redis lease/import run in the disposable backend container: 52 passed, 1 skipped. The app implementation files exercised in the container matched SHA `f2cede7c8dfaff21fb24e99cbe2addf96f298a2e`; the two selected suites cover transaction-time expiry, concurrent reclaim, stale writes, delayed completion, archive recovery, retries and rollback. Opt-in Docker boundary: 1 passed, including bounded concurrency/503, 413 input limit, 256 MiB OOM containment, CPU-bound child timeout, active extraction interrupted by parser-process crash, typed retryable transport failure, restart/rebind, and successful post-restart extraction. | Independent code review of the retry/resource diff found no remaining actionable issue after 503 overload responses were made retryable. Review did not execute this full application stack and is not security acceptance. | No backend Actions run; the remote CI authorization blocker remains. | Disposable synthetic-credential VPS Compose application stack exercised upload → durable storage → worker → parser sidecar → indexing/search through Caddy. Identical re-upload returned the same completed job; a ZIP with supported and unsupported entries produced two indexed files and one skipped entry; a malformed DOCX produced a partial result while preserving/indexing the valid sibling; stopping the parser during import caused a bounded retry and completion after restart. A frontend-to-backend DNS defect was found: generic `backend` resolved to the backend's `lifeai_data` address, which frontend cannot reach. A `backend-internal` alias scoped only to `lifeai_internal` fixed the route; no network membership or policy was widened. Host port-forwarding was not verified in this Docker environment; the internal Caddy → frontend → backend path worked. Lease tests used the deterministic offline test provider and local isolated-parser mode; actual parser-sidecar behavior is covered separately by the Compose flow above and the opt-in boundary test. This is local disposable-stack evidence, not production-equivalent certification; no production secrets or user documents were used. |

## #425 full-stack and lease-fencing verification

The application/parser source tested was exact SHA
`f2cede7c8dfaff21fb24e99cbe2addf96f298a2e`; key lease, parser and import files were
hash-matched between the checked-out source and the running disposable backend image. The
full-stack path used synthetic credentials, a disposable PostgreSQL database, and a
network-isolated CI provider stub. It verified upload through Caddy, frontend, backend,
durable storage, worker, parser sidecar, indexing and search; duplicate upload idempotency;
archive partial-skip and partial-failure outcomes; and retry/recovery after a parser outage.
The host's published-port forwarding did not work in this Docker environment, so that
external host-to-Caddy path is not verified; the internal Caddy route was exercised.

The separate worker/import regression run executed against disposable PostgreSQL and Redis
from the application container with the suite's deterministic offline embedding provider and
local isolated-parser test mode: **52 passed, 1 skipped** (the inherited encrypted-ZIP test
requires the system `zip` binary). The actual parser service was exercised independently in
the full-stack flow and opt-in Docker-boundary test; the regression run did not claim to test
the sidecar. Running these unit/integration suites with the production Compose provider and
parser socket settings initially exposed two test-environment assumptions (provider mock
selection and bounded sidecar overload); rerunning with the documented deterministic test
configuration passed. This does not indicate a parser-service or lease-fencing failure.

The full-stack run exposed and locally corrected a Compose DNS routing defect by assigning
`backend-internal` only to `lifeai_internal` and using it for the frontend's
`INTERNAL_API_URL`. Frontend remains unattached to `lifeai_data`. Remote backend Actions,
external host-port reachability, sustained full-stack resource-exhaustion tests, and formal
independent security acceptance remain unverified. `first_cycle_go=false`; no merge,
deployment, or activation is authorized.

Dependency order is #392 → #402 → #413. PRs #415 and #420 are separate children of
#413; #421 and #422 are evidence-only children of #420. In particular, #420 does not
contain #415.

## #425 network, concurrency, and restart follow-up

Exact implementation commit: `ce4d7de1dfe470cec8b8c965009393fd0b84de1d`. The import-route
source exercised by the concurrency run had SHA-256
`eee0c2fcbb5d0cce9ee741f2076ff8aae95976e703930d7fa0de43dc7118c717`.

The disposable Compose stack published only Caddy on TCP 80/443; the separate test
PostgreSQL container was bound to `127.0.0.1:5432`. Frontend, backend, worker, parser, and
Redis had no host bindings. From frontend, `backend-internal` resolved to the backend's
`lifeai_internal` address, while Redis, Ollama, and `lifeai-db` did not resolve; direct TCP
probes to the Redis and PostgreSQL `lifeai_data` IPs were unreachable. The parser remained
on `network_mode: none`. Caddy shares `lifeai_internal` with backend and can reach its
listener directly, although the Caddyfile routes only to frontend; this residual lateral
reachability is documented, not represented as isolation.

Three simultaneous 512 KiB uploads completed through Caddy, were indexed with matching
checksums, and identical replay returned the existing job. Before the route correction,
same-owner concurrent uploads timed out: a blocking PostgreSQL advisory-lock wait inside
the async route froze the event loop needed by the lock holder to finish. The route now
runs synchronously in FastAPI's thread pool; the regression guard and the running route
were verified. The route source used for this stress run had SHA-256
`eee0c2fcbb5d0cce9ee741f2076ff8aae95976e703930d7fa0de43dc7118c717`.

After restarting Redis, it became healthy and returned `PONG`. After restarting PostgreSQL,
`SELECT 1` succeeded using both the migration role and the derived `mainai_app` runtime role
through the `lifeai-db` data-network alias. The initial post-restart HTTP upload was not
verified because the reused disposable founder credentials returned 401; no further login
attempts were made during that test round. A later fresh disposable-stack run completed the
post-restart HTTP flow and separately injected PostgreSQL loss during advisory-lock wait;
details follow. External host ingress, full-stack resource saturation beyond the measured
512 KiB concurrency case, database or Redis restart during active worker processing, remote
backend Actions, and formal independent security acceptance remain unverified. No
production host, secrets, or user documents were used; `first_cycle_go=false`.

## #425 upload concurrency and restart recovery follow-up

The existing disposable `lifeai_hardening` Compose stack used synthetic credentials and
documents. Seven simultaneous same-owner upload submissions included four unique files,
two identical-content submissions, and an over-compressed archive. The six ordinary
submissions completed, identical content produced one document row, and the archive was
rejected by the compression-ratio guard. A valid eight-file archive completed; its extracted
originals and uploaded archive matched expected SHA-256 checksums.

Worker recovery was exercised by terminating the worker process during archive processing,
then replaying content: processing completed with 160 unique documents. A separate
byte-identical archive replay after a worker crash completed both jobs with 110 unique
document checksums and an intact uploaded-archive checksum. During an active 80-file archive
import, terminating the parser process caused its container restart; the job completed after
restart with 80 unique documents and the uploaded archive checksum intact. PostgreSQL and
Redis/Valkey were also restarted; both became healthy, and authenticated Caddy-proxied
upload, indexing, and exact original download succeeded afterward.

Advisory-lock interruption was tested with a deterministic 497,024-byte text payload whose
SHA-256 was
`f8fc0dada21d34f1c9e11298447c94fc5ece56cafa4233ea454594167e623b26`. While an authenticated
upload waited on the owner's PostgreSQL advisory lock, the backend health endpoint continued
to return HTTP 200. Restarting PostgreSQL caused the request to return HTTP 500; querying
the disposable database and storage confirmed zero import jobs and no content-addressed blob
for that checksum. Retrying the identical bytes after PostgreSQL became healthy completed
indexing, and authenticated original retrieval returned bytes with the same SHA-256. This
demonstrates failure without a partial stored upload and successful retry in this test
topology; it does not prove that the entire worker/lease chain is safe under arbitrary
database failover.

The import endpoint is Founder-only, so cross-owner upload concurrency was not exercised.
These tests used disposable synthetic data; they do not cover external host ingress,
database/Redis restart while a worker is actively committing, sustained resource saturation,
remote backend Actions, or formal independent security acceptance. No production
credentials/documents were used; `first_cycle_go=false`.

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
but source inspection found that parser transport failures were wrapped as generic
`ExtractionIsolationError`. The import layer treated them as terminal per-file failures, while
Uvicorn's bounded-concurrency 503 overload response was also non-retryable. Implementation
SHA `f2cede7c8dfaff21fb24e99cbe2addf96f298a2e` classifies only transport failures and 503
overload responses as `ParserServiceUnavailableError` (an extraction error and transient
`ConnectionError`). Content/format failures remain terminal, and production does not fall
back to local parsing. Retries remain bounded by `ImportJob.max_attempts`; when parser outage
retries are exhausted, linked documents left in `extracting` are terminalized as
`extraction_failed`, preventing orphan reconciliation from retrying them forever. Regression
tests cover the 503 classification, transient archive recovery without duplicate documents,
exhaustion followed by orphan reconciliation, and permanent content failures. The opt-in
Docker test separately kills the parser during an active socket extraction, verifies a typed
transport failure, observes restart/socket recovery, and confirms extraction works afterward.
This is local test evidence, not proof of lease fencing.

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
extraction. The opt-in Docker test additionally verified bounded request concurrency,
memory-limit OOM containment, CPU-bound child timeout/cleanup, and service restart while a
socket extraction was active. It confirmed the interrupted caller received a retryable
transport failure and a subsequent extraction succeeded. These checks do not include
sustained full-application CPU stress or full host/deployment fault injection.

The independent review of the current retry/resource diff found no remaining actionable
issue after the overload-response correction; the review did not execute containers.
Parser/import/worker/lease regressions pass 64 cases with one inherited strict xfail. Remote
backend CI and formal security acceptance remain pending. Render's combined container has
no sidecar and document parsing/imports remain unavailable there until an equivalent
isolated service exists. This is not security acceptance.
