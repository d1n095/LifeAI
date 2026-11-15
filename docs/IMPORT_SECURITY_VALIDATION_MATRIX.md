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
| #425 parser isolated-sidecar follow-up | `2fc520fa7c6e0f1c6550379b23ac852a936d2220` (draft) | #422 at `a4f6819fff04d6919d4b46b1c018ba38aad1ab88` | 50 passed, 1 expected failure across parser/import/recovery tests; real Docker containment test included | Independent code review found no actionable issue; no security acceptance | No remote CI yet | Local Docker test verifies secretless env, distinct PID namespace, network `none`, UID 10001, read-only root, no capabilities, `no-new-privileges`, resource limits, UID-only socket, and real extraction; deployed Compose topology not exercised |

Dependency order is #392 → #402 → #413. PRs #415 and #420 are separate children of
#413; #421 and #422 are evidence-only children of #420. In particular, #420 does not
contain #415.

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

The local opt-in Docker test checks those properties and verifies real text extraction.
The parser/import/recovery suite passed 50 tests with one inherited strict expected failure
that still demonstrates the old same-UID process boundary. Both Compose manifests pass a
YAML parse, but the Compose CLI is unavailable, so the actual Compose deployment topology
has not been started or validated. Render's combined container has no sidecar and therefore
document parsing/imports remain unavailable there in production until an equivalent isolated
service is provided. Exact branch SHA, remote CI, and deployment-equivalent fault injection
remain pending; this is not security acceptance.
