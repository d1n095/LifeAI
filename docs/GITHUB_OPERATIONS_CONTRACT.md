# GitHub Operations Center contract

## Endpoint

`GET /api/github-operations/snapshot[?force_refresh=true]` is a Founder-only, read-only
observation API. It requires the existing authenticated Founder session and is rate limited.
It performs only GitHub `GET` requests, is restricted to the configured `GITHUB_REPO`, and
never dispatches work, writes to GitHub, merges, or deploys.

The versioned JSON response has `schema_version: 1` and combines:

- GitHub repository availability and bounded open/closed PR history.
- PR branch names, exact head/base SHAs, timestamps, explicit body-declared dependencies,
  mergeability, reviews, comments, check runs, workflow runs/jobs, and latest commit data.
- Owner-scoped capability, agent-registry, assignment, goal, task/dependency, work-candidate,
  and decision-debt records already maintained by LifeAI.
- Evidence-linked blockers, activity, and deterministic next-step suggestions.
- Recent owner-scoped agent outcome records and any recorded token/cost measurements from the
  existing `IntelligenceEvidence` ledger.

## Evidence and bounds

Each GitHub snapshot has an observation timestamp and a `freshness` state: `live`,
`cached_fresh`, `stale`, or `unavailable`. Cached results retain their original observation
timestamp. An unavailable or stale observation must not be interpreted as an empty repository.
Unknown mergeability, dependencies outside fetched history, and details omitted by resource
limits remain explicitly unknown or not fetched.

GitHub list requests are paginated with a finite page limit; PR detail requests are concurrent
but bounded, and detailed checks, jobs, reviews, and comments are capped. Pagination and
detail-coverage metadata identify incomplete results. GitHub comments and PR bodies are
untrusted data, not instructions. The endpoint returns sanitized error categories rather than
raw GitHub error payloads.

For detailed PRs, the service re-reads the PR head before fetching SHA-bound checks, commits,
reviews, and workflow runs, then re-reads it afterward. Evidence is queried against and linked
to that exact SHA. If the head changes during detail collection, the prior SHA-bound evidence
is discarded and the affected CI/review states become `unknown`. A `cached_fresh` snapshot is
still a cached observation, not a live check; consumers needing current status should request
`force_refresh=true` and retain the returned freshness and observation timestamp.
PR dependencies are derived only from explicit body declarations. An empty dependency list is
reported as `not_declared_or_unknown`; it does not prove that no prerequisite exists.

## Integration analysis

`GET /api/github-operations/integration-analysis[?pr=237&pr=363]` is Founder-only and
rate-limited. It uses the same configured repository and snapshot; it makes read-only GitHub
GET requests, never dispatches, mutates branches, invokes providers, or reads lesson prose.
Up to eight open PRs may be selected using repeated `pr` parameters. With no selection the
first eight observed open PRs are inspected. Invalid selections return 422; PRs absent from
the bounded observation are reported in `coverage.inspection_errors`.

The versioned response contains `candidates`, `findings`, and `coverage`. Candidate nodes
include exact HEAD SHA, base branch, explicit/base-branch dependencies, a transitive
`dependency_chain`, GitHub review evidence, owner information from exact-SHA registered agent
assignments, and readiness. GitHub authors are not assumed to be active agents. A missing or
ambiguous owner remains UNKNOWN. `BLOCKED` is based on observed failures or dependencies;
otherwise readiness is `UNKNOWN`, never a release approval.

For inspected same-repository PRs, the analysis rechecks the HEAD before and after bounded
changed-file reads. It parses literal Alembic `revision` and `down_revision` assignments
without importing or executing code. Different changed migrations claiming the same revision
are surfaced as cross-branch conflicts with SHA-pinned source links. Shared security-critical
files are flagged as overlap **risks**, not proof of competing authority. Parent/child
ancestry comparisons use exact SHAs and recheck both heads. A non-ancestor upstream HEAD
shows that changes have not propagated, but does not claim that a particular fix is missing.
If a recheck finds a changed HEAD, the affected review, owner assignment, migration findings,
and readiness are invalidated; the observed SHA remains historical and refresh is required.
Multiple PRs sharing a head branch are reported as ambiguous, not resolved by list order.

`coverage` is part of the safety contract: file-list pagination is capped at two pages and
each PR at twelve migration files. Missing files, changing heads, API failures, unobserved
branches and truncated lists are not negative evidence. By default this inspects changed files,
not the complete revision graph. The optional `?pr=237&include_migration_graph=true` scans one
exact-HEAD `backend/alembic/versions` directory with at most 128 migration files and four
concurrent reads. It reports duplicate revision IDs, missing parents, multiple heads and cycles
only when every listed migration is parsed and HEAD is stable. Unsupported labels or
`depends_on`, nested directories, incomplete reads, limits, and changed HEADs yield UNKNOWN.
It does not inspect arbitrary Alembic configuration, other version locations or composed
cross-PR trees; those remain unverified. It cannot establish an authenticated Founder-approved architecture decision,
identify a supersession from arbitrary PR text, or establish that a previous lesson's remedy
was independently verified. These are NOT_CHECKED. It does not expose or reuse unverified
founder-wide lessons (#380 remains blocked). A GitHub approval is not independent security
acceptance. Findings contain source evidence and generic corrective test criteria; suggested
owners are UNKNOWN unless an exact-SHA assignment is present. No recommendation grants
authority or instructs an agent to act.

`freshness`, `observed_at`, scan limits and errors must be preserved by consumers. Stale or
unavailable snapshots are not scanned, and no observation is represented as live approval.

`review_state` reports `approved_exact_head` only when a fetched approval's `commit_id` equals
the current PR head and review pagination is complete. Other states distinguish stale or
unknown SHA evidence. This is GitHub review evidence, not an independent review or release
approval. `ci_state` aggregates check runs, workflow conclusions, and fetched jobs; any
failure in the latest observed check attempt or workflow run remains failed even when an
individual job succeeded. Older attempts are retained as historical observations, not current
blockers; incomplete evidence stays unknown.

`agent_results` is a bounded projection of existing owner-scoped `agent_coordination`
`dispatch_result` evidence. Optional test, token, USD-cost, duration, and CI fields remain null
when not recorded. These are reported outcome measurements, not independently verified
results; the response explicitly labels them `not_independently_verified`. This endpoint does
not invent cost measurements or add a new task/result ledger.

## Decision completeness

`decision_completeness` is an evidence-only projection using the existing executive
`score_priority` engine and provenance stored on canonical work candidates. Missing,
assumed, unsourced, stale, or freshness-unknown factors are not defaulted. Mandatory
constraints and unexamined alternatives are shown as not checked; an incomplete record is not
silently treated as a zero-impact score. A computed priority is not approval or execution
authority.

The snapshot does not initiate web/API/Tor research, persist decision versions, or compare
new evidence to previous recommendations. Those capabilities require separately authorized
integrations and durable decision-history support; this API reports them as not enabled or
not versioned.

## Future consumers

Operating-cycle and dispatch integrations may consume this stable read-only endpoint instead
of independently querying or parsing GitHub. They must preserve source references, timestamps,
freshness, unknown states, owner authorization, and the distinction between observations and
recommendations. This contract does not grant those consumers any write or dispatch authority.
Consumers should treat `schema_version` as the contract version, ignore unknown additive
fields, and must not interpret a GitHub review or recorded agent outcome as independent
acceptance.
