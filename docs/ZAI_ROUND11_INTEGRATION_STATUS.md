# Z.ai Round 11 — integration status (blocked on the artifact)

Recorded 2026-11-19 on `freebuff/founder-alpha-ops-recovery-20261118`.

Z.ai reported Round 11 at `df9150b4` with a Git bundle, tree snapshot, `SHA256SUMS`, integration
manifest, model-gate evidence, Cognitive Core matrix and regression logs. None of that is treated
as present until the commit can be read.

## Probes performed

| Probe | Result |
|---|---|
| `git cat-file -t df9150b4` | not a valid object (not present locally) |
| `git for-each-ref` for `zai` / `round` / `cognit` / `df9150` | no matching ref |
| `gh api repos/d1n095/LifeAI/commits/df9150b4` | HTTP **422** `No commit found for SHA: df9150b4` |
| `find /tmp` and the project for `*.bundle`, `SHA256SUMS*`, `*round11*`, `*df9150*` | only the earlier Round 10 prose notes; no artifact |
| isolated sandbox filesystem | not shared with this host |

One probe to GitHub, no retry loop: without a bundle, patch series or pushed ref there is nothing
new a second probe can reveal.

## Exact transfer requirement (any one)

1. the bundle plus its SHA-256 (`git bundle create zai-round11.bundle <range>`), with `df9150b4`
   inside and an ancestry that includes the reviewed baseline;
2. a `git format-patch` series whose cover letter names the produced SHAs;
3. a pushed scratch ref in `d1n095/LifeAI` (e.g. `refs/zai/round11`).

A "similar branch" is not a substitute; the commit id must match.

## Compatibility plan (prepared, not executed)

1. **Identity and integrity.** Verify the checksum, `git bundle verify`, and that `df9150b4` is the
   delivered head. Refuse substitution.
2. **Isolation.** `freebuff/zai-round11-integration-<date>` from the current head. No merge into the
   default branch, no push over Z.ai's branch, no touching frozen PRs #459/#460.
3. **Migration DAG.** AST-parse every added `backend/alembic/versions/*.py` for `revision` and
   `down_revision`. This tree's head is `0079`; Z.ai's reported `0075–0077` overlap that numbering
   range, so a collision is **expected** and must be resolved by identity (file content and DAG
   position), never by renumbering our reviewed migrations to make Git clean.
4. **Prompt fencing — one canonical boundary.** This tree's implementation is
   `backend/app/rag/prompt_fencing.py` (marker definition, forged-marker defusal, tested). Compare
   Z.ai's C1–C7 contract against it line by line. Keep exactly one implementation; deleting either
   side without a reviewed equivalence argument is forbidden. If Z.ai's is strictly stronger, the
   replacement is a deliberate, tested migration of callers — not two fences.
5. **API/auth/RLS/memory ownership.** Diff routers, owner scoping, RLS policies, provider interface
   and worker dependencies path by path. Report overlap as a conflict.
6. **Security.** Review new dependencies, network calls, subprocess/credential access and any
   provider activation. `first_cycle_go` stays `false`.
7. **Combined acceptance.** Real PostgreSQL/Redis/Ollama, the founder workflow harness, both browser
   specs and Z.ai's own suites on the merged SHA.

## Status

Integration not started; blocked on the artifact. Independent work delivered in this window:
Founder Console + Living Core, and a verified clean-database restore that now boots (see
`docs/FOUNDER_CONSOLE.md` and `docs/FOUNDER_ALPHA_READINESS.md`).
