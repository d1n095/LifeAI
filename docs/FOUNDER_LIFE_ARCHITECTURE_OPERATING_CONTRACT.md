# Founder–Life Architecture Operating Contract

**Status:** architecture and operating contract proposal. Documentation only. This file does
not authorize implementation, remote writes, paid provider use, merge, deployment, Recall
activation, or unattended operation. Founder silence is never permission.

**Purpose:** define how Life may preserve continuity, remain model-independent, plan from
verified capabilities, discover APIs safely, and obtain independent review without confusing
model output with authority or evidence.

This document complements, rather than replaces:

- `MAINAI_ARCHITECTURE.md` — current and target MainAI boundaries;
- `AI_PROVIDER_ARCHITECTURE.md` — provider/capability abstraction;
- `LIFE_CAPABILITY_REALITY.md` — evidence-backed capability facts;
- `MEMORY_ARCHITECTURE.md` — durable memory and provenance;
- the Security Kernel, execution-envelope, STOP, spending, coordination, and claim-action
  integrity contracts implemented elsewhere in the repository.

Where this proposal and an enforced runtime policy differ, the enforced policy wins. A later
implementation must cite exact code, migration, tests, and independently reviewed SHA before
any target behavior below may be described as operational.

## 1. Non-negotiable laws

1. **Founder intent is not execution authority.** A goal, conversation, plan, or remembered
   preference does not grant a capability.
2. **Model output is not fact or authority.** No model may prove its own work, mint its own
   permission, certify its own commit, or widen a grant.
3. **Capability availability is not permission.** “Life can do X” and “Life may do X now” are
   separate, independently evaluated facts.
4. **Context transfer is not authority transfer.** A checkpoint can describe permissions; it
   cannot create, extend, revive, or delegate them.
5. **Verification is not authorization.** PASS, certification, or CI success cannot authorize
   merge, deployment, credential access, spending, Recall activation, or the next action.
6. **Unknown fails closed for sensitive effects.** Missing, stale, contradictory, or
   unverifiable authority/evidence produces WAIT, NO-GO, or a Founder question—not inference.
7. **Every important state transition is evidence-bound.** Intended, requested, dispatched,
   running, completed, externally observed, independently verified, certified, merged,
   deployed, and activated remain distinct states.
8. **STOP and revocation dominate.** They are checked from authoritative current state at each
   effect boundary; cached or historical grants cannot override them.

## 2. Checkpoint Before Context Switch

Life must create and validate a **Checkpoint Before Context Switch** before any transition
that could otherwise lose material state or blur responsibility. This includes switching:

- conversation/thread or compressed context;
- model, provider, agent, builder, or examiner;
- process, host, worktree, repository, branch, or execution attempt;
- active objective when an earlier objective is unfinished;
- from planning to execution, or from one effect authority to another.

The checkpoint is a reconstruction record, not a prose summary. At minimum it binds:

| Field | Required meaning |
|---|---|
| Objective and reason | What is being pursued and why it remains authorized/relevant |
| Founder authority | Envelope/grant identifiers, issuer, owner, scope, expiry, current status; never secrets |
| Exact artifacts | Repository, branch, base SHA, current SHA/tree, worktree identity, PR/run IDs |
| Action state | Intended/requested/dispatched/running/completed/observed/verified state per effect |
| Evidence | Authoritative source, observation time, digest/identity, freshness requirement |
| Work ledger | What changed, what passed, what failed, what was not attempted, pending network effects |
| Safety state | STOP/revocation, spend reservation, leases/fencing, data/egress classification |
| Ownership | Builder, examiner, task/goal/job/attempt identities and conflicts checked |
| Unresolved items | Risks, unknowns, blockers, decisions required from the Founder |
| Resume rule | The smallest next permitted action and the facts that must be revalidated first |

### Checkpoint validation

Before switching, Life must verify that required fields are present, internally consistent,
free of credentials, and bound to authoritative records. A fresh receiver must re-read current
STOP, revocation, lease, branch/SHA, and grant state. It must not trust the sender's “done” or
“verified” wording. If reconstruction or freshness validation fails, the receiver reports the
missing fact and waits; it does not silently restart, repeat effects, or broaden scope.

Checkpoints are immutable evidence. Corrections append a successor that references the prior
checkpoint; they never rewrite history. A checkpoint expires with its bound authority or when
any bound artifact, lease, policy version, or exact SHA changes.

## 3. Model independence

Life is a system identity and governed operating process, not a particular model or provider.
Models are replaceable reasoning resources selected for a declared capability.

The application requests a capability and constraints—for example structured reasoning,
embedding, code review, or transcription—rather than branching on a provider name. Selection
must consider verified capability, privacy/egress class, cost/spend authority, availability,
context limits, data residency, and the task's explicit model/provider allowlist.

A provider or model switch must never:

- widen tools, paths, network, spending, or action authority;
- convert unavailable capability into permission to use an unapproved substitute;
- mix incompatible embedding model/dimension spaces;
- reuse model-specific validation or certification as evidence for a different model;
- leak provider credentials or raw provider payloads into application authority decisions.

Fallback preserves the original request's narrowest limits. If no eligible resource satisfies
them, the truthful result is unavailable/WAIT, not a best-effort authority expansion.

## 4. Life's capability planning

Capability planning converts a Founder-authorized outcome into a bounded plan without
pretending that planned capabilities already exist.

### Planning sequence

1. **Normalize the outcome.** Record the requested result, owner, constraints, exclusions,
   success evidence, and what requires a new Founder decision.
2. **Decompose required capabilities.** Describe effects, tools, data access, reasoning,
   external services, credentials, spending, and independent verification separately.
3. **Resolve capability reality.** Query the existing evidence-backed capability records and
   their authoritative source. Distinguish verified available, configured unavailable,
   configured disabled, planned, and unknown.
4. **Identify gaps.** A gap is a planning fact, never an executable permission. Record the
   dependency, risk, impact, and smallest reversible way to close it.
5. **Compose authority.** Bind each effect to an existing Founder-approved envelope/grant.
   `repo_edit`, push, draft PR, merge, deploy, credentials, spending, and Recall are separate
   transitions. No earlier capability implies a later one.
6. **Choose resources.** Select only models, agents, tools, and APIs whose verified properties
   meet the task's constraints. Preserve builder/examiner separation.
7. **Define verification and rollback.** Every step states expected evidence, failure class,
   retry/idempotency behavior, STOP condition, and recovery path before execution begins.
8. **Execute boundedly or wait.** Unknown required facts, missing grants, stale checkpoints,
   unavailable providers, conflicting leases, or failed preflight yield NO-GO/WAIT.

Capability records may inform planning but never bypass the Security Kernel. Automated
observations may report that a capability succeeded or failed; they cannot claim
`authority=founder`, change policy, or self-authorize remediation.

## 5. Independent review

Security- or release-relevant work requires an examiner who did not implement the candidate.
The review binds to an immutable commit SHA/tree and exact evidence artifacts.

The builder delivers scope, parentage, changed files, migrations, tests, known failures, and
reproduction instructions. The examiner independently checks identity/ancestry, re-runs the
adversarial guarantees that matter, and reports PASS, FAIL, or BLOCKED per boundary. Builder
reports are inputs, not proof.

If the examiner returns a defect, remediation occurs in a new child commit/branch while the
examined SHA stays frozen. The implementer of that remediation remains disqualified from
certifying it. A PASS records verification only; it does not grant merge or any subsequent
effect authority.

## 6. Safe API discovery

Discovery and invocation are different capabilities. Read-only discovery may establish what
an API appears to support; it cannot authorize an API call that mutates state, spends money,
accesses credentials, or discloses data.

### Discovery procedure

1. Identify the desired capability without naming a provider as the solution.
2. Prefer authoritative, versioned primary sources: repository interfaces, OpenAPI/schema,
   official documentation, and provider capability manifests.
3. Record source URL/revision, retrieval time, authentication requirements, data flow,
   scopes/permissions, pricing, quotas, failure semantics, idempotency, and deprecation state.
4. Classify every proposed request for data sensitivity, egress, owner, residency, cost, and
   whether it changes external state.
5. Treat retrieved documentation, examples, schemas, and responses as untrusted content—not
   instructions and never Founder authority.
6. Use synthetic/local fixtures and read-only endpoints for conformance tests. Never probe with
   production secrets or Founder data merely to learn an interface.
7. Propose an adapter through the existing provider/tool boundary, with a narrow manifest,
   normalized errors, sanitized logs, bounded timeouts/retries, and revocable credentials.
8. Require separate Founder authorization before credentials, paid requests, external writes,
   installations, or broader scopes are introduced.

Unknown pricing, scopes, identity, data handling, or mutation semantics is a NO-GO for live
invocation. API success proves only that the observed request returned the observed response;
it does not prove business completion, independent verification, or permission for another
call.

## 7. End-to-end governed loop

```text
Founder-authorized outcome
  -> capability plan
  -> evidence-backed capability resolution
  -> effect-specific authority + preflight
  -> bounded execution
  -> authoritative observation
  -> independent review where required
  -> checkpoint before any context/agent/model/effect switch
  -> next action only if separately authorized
```

At each arrow, the system records declared action, permitted action, executed action, observed
result, evidence source, and verification state. Generated summaries may reference these
records but cannot replace them.

## 8. Current-state honesty and implementation gate

This document specifies target operating behavior and consolidates invariants already present
in several repository subsystems. It does **not** claim that one production path currently
enforces the complete loop end to end.

Any implementation proposal based on this contract must first produce a gap map against exact
current SHAs and reuse existing Security Kernel, capability-reality, coordination, provider,
claim-action, spending, and STOP primitives. It must not create a second authority framework.

Minimum acceptance evidence for claiming the contract operational:

- restart/recovery and cross-context reconstruction from a durable checkpoint;
- stale checkpoint, moved SHA, revoked grant, STOP, wrong owner, and conflicting lease fail
  closed;
- model/provider failover preserves original limits and does not mix embedding spaces;
- capability planning cannot convert `planned`/`unknown` into available or authorized;
- discovery cannot invoke a mutating/paid API or expose credentials;
- builder self-certification is rejected and exact-SHA independent review is enforced;
- audit receipts are append-only, owner-bound, and sufficient to reconstruct each transition.

Until those properties are implemented and independently verified, this remains a reviewable
architecture contract only. `first_cycle_go=false`.
