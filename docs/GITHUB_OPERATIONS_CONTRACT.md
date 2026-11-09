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
