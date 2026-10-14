# MainAI Git transport and credential-authority audit

Date: 2026-10-14

Branch: `codex/mainai-git-transport-credential-audit`

Immutable production base: `2fbe20aacf1203fc0e16d216ef55b666b2181619`

Composition inputs inspected: Path A through task-authority candidate `e8914c6`,
Track B `da4d96b`, GitHub read-only #263 `e33f494`, and the accepted activation
contract `8a5bdaf` (specification only).

Decision: **NO-GO. A durable push grant cannot protect a credential that is already
available outside the one authorized effect.**

This audit is evidence only. It changes no production code, credential, GitHub
setting or frozen candidate, and performs no live GitHub mutation. The executable
local oracle is `docs/evidence/mainai_git_transport_reference_probes.py`.

## 1. Current credential and transport graph

### Application credential

`app.config.Settings.github_token` reads `GITHUB_TOKEN`. `GitHubClient` retains the
settings object, constructs `Authorization: Bearer <token>` for every REST request,
and uses the same token for reads and writes. The process therefore holds a broadly
reusable repository credential for its whole lifetime; expiry, cancellation, task
completion and push-grant state do not affect it.

Every current REST mutation primitive is public on `GitHubClient`:

- `create_branch` (`POST /git/refs`);
- `create_or_update_file` (`PUT /contents/...`);
- `create_pull_request` (`POST /pulls`);
- `update_pull_request` (`PATCH /pulls/{number}`);
- `create_blob`, `create_tree`, `create_commit`;
- `update_ref` (`PATCH /git/refs/heads/...`);
- composite `commit_multiple_files`.

The generic `_request` accepts arbitrary method/path and performs no effect-authority
check. Caller-level `github_write_enabled` checks are convention, not a complete
transport boundary.

### Job worktree Git credential

`mainai_execution/worktree.py:create_task_worktree_sync()` converts the token to:

```
https://x-access-token:<GITHUB_TOKEN>@github.com/<owner>/<repo>.git
```

and persists it with `git remote add origin`. It therefore lands in the worktree's
`.git/config`. Every later `fetch` and `push` uses that remote. No push grant,
cancellation state or lease is needed by a direct `git push` executed in that
directory.

The token survives while the worktree directory survives. `release_worktree()`
deletes the directory on an orderly terminal path, but a crash, stopped worker,
dead job, cancelled task before cleanup, or recovery interval leaves it available.
`rebind_worktree_to_job()` deliberately preserves the checkout and only changes the
ownership marker, so a recovered attempt inherits the same credential. A process
restart does not invalidate the URL. Token revocation/expiry at GitHub is the only
external event that makes the persisted secret unusable.

### Track B subprocess environment

Track B uses a minimized explicit environment and isolated HOME, which correctly
prevents accidental inheritance of the Founder's personal agent stores. However,
`LIFE_AGENT_ADAPTER_ENV_ALLOWLIST__<KEY>` can currently name arbitrary environment
variables except HOME/config-store names. It does not prohibit `GITHUB_TOKEN`,
`GH_TOKEN`, `GIT_ASKPASS`, `SSH_AUTH_SOCK`, `GIT_CONFIG_GLOBAL`,
`GIT_CONFIG_SYSTEM`, `GIT_CONFIG_COUNT` or credential-helper configuration.

Even with an empty environment, a CLI launched in the current job worktree can read
the credential-bearing `.git/config`. A write-capable agent can invoke `git push`
directly. A nominally read-only CLI should not be assumed incapable of reading that
secret; permission flags are not a secret-isolation boundary.

### Credential helpers and parent repositories

Current `_run_git()` calls do not set `GIT_CONFIG_NOSYSTEM`, a private empty
`GIT_CONFIG_GLOBAL`, `credential.helper=`, `core.askPass=`,
`GIT_TERMINAL_PROMPT=0`, or `GCM_INTERACTIVE=Never`. Git may therefore consult
system/global credential helpers and platform keychains.

The Path A job checkout is initialized as an independent repository, but it still
consults ambient system/global config. Supervisor worktrees are created by
`git worktree add` from the worker source repository. Their `.git` file points into
the parent's common Git directory and they inherit its remote URLs, credential-
helper settings and relevant repository config. The Supervisor currently promises
local-only operation, but an arbitrary subprocess running there can discover and
use inherited remote configuration unless transport is separately sandboxed.

### Other subprocess surfaces

- verification plans execute Python/pytest in a worktree;
- Development Operator executes allowlisted commands;
- Track B starts external coding-agent CLIs in an assigned worktree;
- any compromised/arbitrary executable in those contexts can invoke Git itself.

An effect-authority predicate in `push_worktree_branch()` cannot contain those
processes if a reusable credential remains readable from config, environment,
helper, socket or parent checkout.

## 2. Survival and bypass conclusions

| Event | Current credential outcome | Authority outcome |
|---|---|---|
| Grant expiry | token/config unchanged | direct push still possible |
| Task completion | removed only if orderly `release_worktree()` runs | no reliable invalidation |
| Cancellation | no token scrub or remote rewrite at cancellation | push remains possible until cleanup/revocation |
| Worker restart | env is reloaded; on-disk URL persists | resumed worker or other process can reuse it |
| Worktree recovery | checkout is rebound in place | credential explicitly survives recovery |
| Parent worktree reuse | common Git config remains shared | helper/remote changes propagate |

The intended one-shot grant can currently be bypassed by direct `git push`, a
resumed/recovered worker, inherited config/helper state, an allowlisted credential
environment variable, a legacy REST path, or an arbitrary subprocess. A successful
no-op retry is still a second unauthorized remote request; idempotence is not
authority.

## 3. Smallest safe transport design

Do not introduce another orchestrator. Add a single remote-effect boundary beneath
the existing Path A executor and existing `GitHubClient`.

### A. Credential-free checkouts

1. Persist only canonical credential-free HTTPS URLs:
   `https://github.com/<owner>/<repo>.git`.
2. Reject URLs with userinfo, non-HTTPS schemes, unexpected hosts, redirects to an
   unapproved host, or repository identity mismatch.
3. For every Git subprocess outside the effect executor, supply a sanitized Git
   environment: `GIT_CONFIG_NOSYSTEM=1`, private empty `GIT_CONFIG_GLOBAL`,
   `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=Never`; clear askpass, SSH command/socket,
   credential variables and `GIT_CONFIG_COUNT`; additionally pass
   `-c credential.helper=` and `-c core.askPass=`.
4. Extend Track B's forbidden forwarded environment names/prefixes to all GitHub/Git
   secrets and config injection variables. Do not allow secret-bearing values merely
   because a Founder listed their names.
5. Audit the common Git directory for linked Supervisor worktrees. Do not expose the
   source checkout or its credential/helper config to an agent sandbox.

Read-only public Git can then fetch anonymously. A private repository must use a
separate read-only transport capability, injected only into the controlled fetch
process; it must never be stored in config or exposed to agents. Read-only REST
discovery similarly uses a read-only token held by the service boundary.

### B. One authorized remote effect

Implement one `RemoteEffectExecutor` used by Git push and every mutating REST call.
It receives a durable, already-authorized effect record, never a raw caller boolean.

The existing activation-security `mainai_push_grants` record must bind:

- grant id, authority version and immutable preflight receipt;
- owner, goal, task, job, attempt and lease generation;
- canonical physical worktree identity;
- GitHub installation/account, canonical repository and allowed host;
- exact source branch and destination ref;
- approved base SHA, expected current remote tip and exact local commit SHA;
- single effect kind (`push_branch` only), issue/expiry/cancel/consume timestamps.

In one database transaction immediately before network I/O, lock the grant and its
job/envelope, re-read cancellation/lease/authority, verify all bindings and change
`authorized -> consumed`, creating an immutable effect-attempt receipt. Consumption
is irreversible. No retry, recovery or takeover may transfer it.

The transport service then mints/obtains a short-lived GitHub App installation token
whose expiry is no later than the grant expiry. A long-lived PAT cannot itself be
made one-shot; if a PAT is retained as the root credential it must be confined to a
separate effect service inaccessible to agents and never injected into the worker.
For strongest bounded lifetime, mint an installation token per effect and revoke it
immediately after the attempt.

For Git push, invoke a fixed argv with the exact credential-free URL and exact
refspec, never the mutable `origin` alias. Provide the token only to that child
through a private askpass/credential channel outside the worktree, with a sanitized
environment, no shell, no inherited helpers, and unconditional cleanup/revocation
in `finally`. Avoid secrets in argv, logs and receipts. Immediately observe the
exact destination ref through an independent read path.

GitHub cannot cryptographically restrict an installation token to one branch or one
request. The one-shot property therefore depends on process isolation plus the
durable grant gate and repository ruleset. If the token is exposed to an agent,
one-shot safety is unprovable and preflight must return NO-GO.

### C. Common predicate

The predicate must run both at the high-level call site and at the lowest mutation
transport boundary. `GitHubClient._request()` must classify all non-GET/HEAD requests
and refuse them without an effect context whose method, endpoint, repository,
branch/PR and grant agree. This makes a newly added call site fail closed.

`repo_edit`, `push_branch`, and `open_pr` remain different grants. A push token must
not authorize PR creation. A draft-PR effect uses its own atomically consumed grant
and installation token/REST request.

## 4. Crash and recovery state machine

```
AUTHORIZED --atomic claim--> CONSUMED/NOT_SENT
  -> SENT/UNKNOWN -> OBSERVED_SUCCEEDED | OBSERVED_NOT_APPLIED | CONFLICT
  -> transport failure before send -> OBSERVED_NOT_APPLIED
```

- Crash before atomic consumption: grant remains usable only by its bound attempt,
  while unexpired, uncancelled and with unchanged authority/preflight/base/local SHA.
- Crash after consumption but provably before network: grant stays consumed. A human
  or policy may issue a new grant only after a fresh preflight and exact state check.
- Timeout/crash during or after network: mark outcome unknown. Never retry the push.
  Read the exact remote ref independently.
- Remote tip equals granted local SHA: record externally observed success.
- Remote tip still equals the granted expected prior tip: record not-applied; a new
  grant/new attempt is required.
- Any other tip, missing/inconsistent evidence or untrusted redirect: conflict and
  manual review.
- Crash after push but before receipt commit follows the same observation path. The
  old grant and token are never revived.
- Cancellation after consumption cannot undo a remote write; it blocks every later
  effect and triggers observation/reconciliation only.

## 5. Mutation call-site inventory

All of the following must use the common predicate and effect executor:

1. `mainai_execution/worktree.py:push_worktree_branch` (`git push`).
2. `mainai_execution/execution_job.py:_finalize_repo_edit` (push caller).
3. `mainai_execution/execution_job.py:_handle_open_pr`
   (`create_pull_request`).
4. `agent_orchestration.py:prepare_github_pr`: legacy `create_branch`, Contents
   write and draft PR. Keep the whole legacy path disabled for activation v1.
5. `integrations/github_client.py`: `create_branch`, `create_or_update_file`,
   `create_pull_request`, `update_pull_request`, `create_blob`, `create_tree`,
   `create_commit`, `update_ref`, `commit_multiple_files`, and the generic `_request`
   backstop.
6. Any future GitHub mutation added anywhere in the repository: static policy must
   reject direct non-read HTTP and `git push` outside the effect executor.

Read-only but credential-sensitive call sites must use the separate read path:
`get_ref`, `get_pull_request`, `list_pull_requests_for_head`, `list_check_runs`,
#263 discovery methods, job-worktree `fetch`, recovery inspection/salvage reads and
CI polling. `development_supervisor/production_worktree.py` remains local-only and
must receive no remote credential.

## 6. Required production tests

The reference oracle covers the required eight adversarial cases, but production
acceptance must exercise real boundaries with fake local remotes and intercepted
HTTP, never real GitHub writes:

- expired grant and expired/revoked token cannot be reused;
- direct push from job, Track B and Supervisor worktrees receives no credential;
- remote URL/userinfo/host/repository/ref substitution is rejected;
- a consumed grant cannot authorize a second push;
- retry/recovery/rebind cannot copy or revive the grant;
- system/global/local credential helpers and askpass/config injection are ignored;
- process restart preserves consumed state and unknown-outcome reconciliation;
- unrelated/legacy goals cannot write when global GitHub writes are enabled;
- ambiguous push outcome follows exact-tip reconciliation;
- secrets never appear in config, argv, logs, receipts, exceptions or agent env;
- open-PR authority cannot push, and push authority cannot open a PR;
- static scan permits mutation only inside the effect boundary.

Run the oracle with:

```
python3 docs/evidence/mainai_git_transport_reference_probes.py
```

Expected result: `8/8 transport-security reference probes passed`.

## 7. Migration head re-check

The actual Founder Alpha, Path A task-authority, Track B and #263 inputs all retain a
single head: `0088_erasure_completion_phase`. None adds a migration.

Separate frozen lineages still collide:

- Continuous Conversation: `0089_continuous_conversation_foundation` ->
  `0090_cc_thread_memory` -> `0091_cc_p1_authority` ->
  `0092_cc_one_active_decision`;
- Founder Sovereignty: the shared 0089 -> `0090_founder_sovereignty` ->
  `0091_fs_p1_authority` -> `0092_fs_p1_root_of_trust`.

Therefore `0089_mainai_activation_security` remains valid only for an activation
candidate explicitly rooted at 0088 and excluding both lineages. Include push grants,
remote-effect attempts and preflight receipts in that same atomic activation-security
migration. If another lineage is deliberately composed first, allocate the next
linear revision from its actual single head; do not create a parallel head or guess a
number in advance.

## 8. Updated composition-manifest notes and GO delta

Add these mandatory notes to the frozen composition manifest when it is instantiated:

- `transport_credentials_persisted = false` must be proven by config/env/helper scans;
- `remote_effect_executor_sha` and its independent verdict are required inputs;
- push and draft-PR grants identify distinct effect kinds and consumption receipts;
- private-repo read credentials are separate, read-only and absent from worktrees;
- Track B and Supervisor subprocesses receive no Git/GitHub credential or helper;
- ambiguous effects reconcile exact refs and never replay consumed grants;
- GitHub App installation token isolation/revocation, or an explicitly equivalent
  service boundary, is required; a worker-visible PAT yields NO-GO;
- static mutation-call-site coverage and all production adversarial tests are green.

Until those facts and the existing composition gates are independently verified on
the final exact SHA, creation of `mainai/integration-founder-supervised-v1` remains
**NO-GO**.
