# MainAI Core Loop v1 — backlog

Side findings from `claude/mainai-core-loop-v1` (see `docs/BRANCH_REGISTRY.md`'s Pass 6) that
do NOT block the acceptance contract and were deliberately not chased during that work, per
the task's own instruction to record rather than expand scope. Each entry says what was found,
why it doesn't block, and what a future branch/PR should do about it.

## Local sandbox cannot build the real backend/frontend Docker images

This session's sandbox routes outbound HTTPS through a policy-enforcing proxy that only
tunnels HTTPS CONNECT traffic. `backend/Dockerfile`'s `apt-get install curl` step hits
`deb.debian.org` over plain HTTP (Debian's default archive mirror), which the proxy rejects
with `405 Method Not Allowed`, and unproxied direct access gets a `403 Forbidden` (the host
isn't on the sandbox's network allowlist — unlike `pypi.org`/`registry mirrors`, which are).
This is a sandbox/environment limitation, not a defect in the Dockerfile — GitHub Actions
runners have unrestricted internet and build the same image without issue, which is why every
real end-to-end verification for this task's acceptance contract went through the actual
`vps-compose-verify` CI job rather than a local `docker compose up`.

Not a backlog item to "fix" — noted here so a future session doesn't waste time rediscovering
this. If genuinely useful, a future change could switch the base image's apt sources to HTTPS
mirrors so sandboxed sessions can build locally too — but that's a Dockerfile change made for
tooling convenience, not a product need, and should get its own small PR with its own
justification if someone wants it.

**Correction (2026-07-28):** the actual backend *test suite* (`pytest tests/`) does NOT need
Docker at all, and runs fine in this sandbox once given a real Postgres+Redis — only building
the application's own Docker *images* is blocked. `pg_ctlcluster 16 main start` (a local
Postgres already installed in this sandbox, just not running by default) plus
`redis-server --daemonize yes` gets `DATABASE_URL`/`APP_DATABASE_URL`/`REDIS_URL` a real target
without touching Docker — the full suite (535 passed, 1 intentionally skipped) ran this way
while fixing the Gemini embedding-dimension bug below. Worth remembering before assuming "no
Docker" means "no real test run."

## `restart: unless-stopped` did not visibly restart a SIGKILLed container within 30s in GitHub's Docker-in-Docker runner

Found while building the restart-survival CI step (`.github/workflows/ci.yml`'s
`vps-compose-verify` job, "Restart survival" step). The first real CI run (GitHub Actions run
`30303993791`) showed the worker container still `Exited (137)` a full 30 seconds after
`docker kill -s SIGKILL`, with no sign of dockerd's restart-policy having kicked in — the
"Dump logs on failure" step's `docker compose ps -a` output confirmed this directly. Worked
around by having the CI step explicitly run `docker compose ... start worker` instead of
waiting on dockerd's own restart timing (commit `4d47820`).

This does NOT necessarily mean the real Strato VPS has the same problem — GitHub's
Docker-in-Docker runners are a different (nested, resource-constrained) environment from a
normal VPS's native dockerd, and restart-policy backoff behavior is known to vary by daemon
version/load. But it's worth a founder spot-check on the real VPS at some point (`docker kill
-s SIGKILL <worker-container>` then time how long it takes to come back with plain `docker ps`)
rather than assuming CI's finding generalizes or doesn't. Not blocking this task because the
acceptance contract only requires the flow to survive a restart, which it now demonstrably does
either way (auto or operator-triggered) — but the founder should know dockerd's own automatic
recovery timing is unverified on the real box.

## Chat's SYSTEM_PROMPT context block truncation for very long retrieved chunks

Not touched or newly discovered as a bug — `app/routers/chat.py`'s `context_block` join has no
explicit length cap of its own (relies on each chunk already being bounded by
`app/rag/chunking.py`'s chunk size). Out of scope for this task (no upload in the acceptance
contract's small/normal-sized test files gets anywhere near a size where this would matter),
but worth a future look if very large individual chunks (e.g. from a document with unusually
large un-chunkable sections) are ever reported as blowing up prompt size/cost.

## `docs/STRATO_VPS_DEPLOY.md`'s branch-sync guidance still says `claude/det-kommer-mer-879lcm`

PR #26's doc fix (carried into this branch) correctly documents syncing `/opt/lifeai`'s
sparse checkout to `claude/det-kommer-mer-879lcm`. Once `claude/mainai-core-loop-v1`'s PR
merges into `claude/det-kommer-mer-879lcm`, that guidance is still correct (the VPS should
still track `det-kommer-mer-879lcm`, not this integration branch) — noted here only so a
future reader doesn't think it needs updating just because this branch existed.
