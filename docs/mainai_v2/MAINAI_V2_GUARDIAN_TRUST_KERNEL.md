# MainAI V2 — Guardian / Trust Kernel (Stage V2-B)

**Status:** design-only. Does not modify PR #245 / candidate SHA `818dfb732da47901eb5ae06ffdd9c829fe00c4c5`.
Assumes `docs/mainai_v2/MAINAI_V2_ARCHITECTURE_MAP.md` (V2-A) as shared vocabulary — read that
first. This document answers §V2-A/§2's Guardian box concretely.

## 0. The core design decision, stated once

**Guardian is deliberately dumb.** It runs no model, no "intelligence," no judgment calls. It
is a small set of mechanical, auditable checks and durable state transitions — the kind of
code a human reviewer can read start to finish in one sitting and be *certain* about, the same
way this session's own `app.workforce.kill_switch` (833 lines including tests, one file,
zero LLM calls) was certain enough to independently verify with real two-connection race
tests. Guardian's entire job is: **hold root authority, and never be talked out of using it
by anything that runs above it** — including MainAI herself, however convincing her reasoning
sounds. `MODEL OUTPUT != AUTHORITY` applies to Guardian's relationship with MainAI even more
strictly than it applies anywhere else in the stack: Guardian must be structurally incapable
of accepting "MainAI's own judgment that everything is fine" as an input to any of its
decisions. Every Guardian decision is grounded in one of: (a) a durable DB row an authorized
caller wrote, (b) an owner-signed identity assertion, (c) a hard-coded ceiling. Never a
sentence MainAI generated.

## 1. Guardian's exact responsibilities vs. what stays with MainAI

| Responsibility | Owner | Why |
|---|---|---|
| Root authority (the ceiling nothing below can exceed) | **Guardian** | Must not be self-modifiable by the thing it constrains |
| Emergency containment (stop everything now) | **Guardian** | Must work even if MainAI's own reasoning is compromised |
| Vault boundary (what may ever leave, under what policy) | **Guardian** | Highest-value asset; policy changes here are rare and should be mechanically gated, not conversationally negotiated |
| Network authority (what MainAI/agents may ever reach) | **Guardian** | Same reasoning as Vault — a network allow-list is a security primitive, not a UX preference |
| Agent authority ceiling (the max any single department/agent could ever be granted) | **Guardian** | Bounds MainAI's own delegation power — MainAI can grant *less* than the ceiling freely (existing `execution_envelopes`/`WorkforceAssignment` machinery), never more |
| System integrity (is the runtime itself the one we expect) | **Guardian** | Root-of-trust question, must not depend on the thing being measured |
| Security policy enforcement (final gate, not advisory) | **Guardian** | See §4 |
| Recovery entry point | **Guardian** | The one door that must still open when everything above it is untrusted |
| Owner identity verification | **Guardian** | See V2-G — Guardian is the consumer of Sovereign Identity, not its own separate identity system |
| Interpreting the owner's actual intent, holding conversation, executive reasoning, workspace control, delegating work to departments | **MainAI** | Everything that benefits from judgment, context, and language understanding — explicitly NOT Guardian's job |
| Proposing new authority scopes, proposing capability claims, proposing detection rules (Sentinel) | **MainAI / departments** | Proposal is cheap and expected to be wrong sometimes; Guardian is where wrong proposals stop, matching `propose_execution_scope()` vs. `authorize_execution_scope()` |

## 2. Containment mechanism: generalize `workforce_authority_epoch`, don't build a second one

**Decision: Guardian's containment IS the existing epoch-based mechanism, generalized.** Not
a separate, parallel "even more privileged" layer with its own state machine.

Reasoning: `app.workforce.kill_switch`'s `workforce_authority_epoch` table already has the
exact shape Guardian needs — a durable, DB-backed, per-scope (owner + global) epoch that a
`SELECT ... FOR SHARE` (grant path) and `SELECT ... FOR UPDATE` (stop path) serialize against,
so "stop" and "grant new authority" can never race each other regardless of which process or
thread either happens in. This was proven correct under real adversarial two-connection
testing tonight (PR #243) — it is the single most rigorously verified piece of authority code
in this entire codebase right now. Building a *second*, Guardian-specific epoch mechanism
above it would mean maintaining two authority-serialization primitives that must never
disagree, which is exactly the "reinvented the same check, slightly different, slightly wrong"
failure mode V2-A §0 warns about (found six independent times in one night already).

Concretely: **generalize the scope key.** Today `workforce_authority_epoch.scope_key` is
`'GLOBAL'` or an owner UUID, and only workforce assignments consult it. V2-B extends this to a
richer scope taxonomy Guardian actually needs to contain:

```python
# Illustrative, non-imported prototype -- not wired into any runtime path.
from dataclasses import dataclass
from enum import Enum

class ContainmentScope(str, Enum):
    GLOBAL = "GLOBAL"                    # every owner, everything -- true kill-all
    OWNER = "OWNER"                       # this owner's MainAI entirely
    WORKFORCE = "WORKFORCE"               # existing scope, unchanged -- workforce assignments only
    NETWORK = "NETWORK"                   # MainAI's own network egress (new)
    PROVIDER = "PROVIDER"                 # external AI provider calls (new)
    VAULT = "VAULT"                       # Vault read/disclosure (new)
    SENTINEL_RESPONSE = "SENTINEL_RESPONSE"  # Sentinel's own defensive-autonomy actions (new, see V2-D)

@dataclass(frozen=True)
class ContainmentRequest:
    scope: ContainmentScope
    owner_id: "uuid.UUID | None"          # None only valid for GLOBAL
    reason: str
    requested_by: str                     # "guardian_self" | "mainai_self_isolation_request" | "owner_explicit" | "sentinel:<detector_name>"
```

Same table, same `stopped`/`epoch` columns, same `SELECT ... FOR SHARE`/`FOR UPDATE`
serialization primitive, one more dimension (`scope`) in the composite key instead of just
`owner_id`. A Guardian-level `GLOBAL`/`OWNER` stop transitively blocks every narrower scope
(a `WORKFORCE` grant check must also check `OWNER` and `GLOBAL` aren't stopped) — this is a
straightforward extension of the existing `assert_grant_allowed()` two-row-lock pattern
(today: GLOBAL row then owner row; tomorrow: GLOBAL row, then owner row, then the specific
scope row) with the same deadlock-free lock-ordering proof already established.

## 3. Authority-boundary API sketch

```python
# Illustrative, non-imported prototype code -- not wired into any runtime path yet.
# Builds directly on app.workforce.kill_switch's proven primitives (generalized per §2).

def request_self_isolation(db: Session, *, owner_id: uuid.UUID, mainai_reason: str) -> "ContainmentReceipt":
    """MainAI calling this is a PROPOSAL, never a decision -- matches propose_execution_scope()
    vs authorize_execution_scope(). Guardian evaluates the request against its own mechanical
    rules (rate limits, does the reason match a known self-diagnostic category, is this the
    Nth request in a suspiciously short window suggesting the reasoning itself is compromised)
    and independently decides whether to actually contain. MainAI cannot force containment by
    asserting urgency, and cannot prevent it either -- once Guardian decides yes, it happens
    regardless of any further argument from MainAI."""
    ...

def guardian_set_authority_ceiling(
    db: Session, *, owner_id: uuid.UUID, scope: "ContainmentScope",
    max_risk_level: str, requires_owner_ack: bool,
) -> "AuthorityCeiling":
    """The ceiling MainAI's own delegation (existing execution_envelopes/WorkforceAssignment
    machinery) can never exceed. Only callable with a real owner-identity-verified request
    (V2-G) or Guardian's own hard-coded defaults -- never by MainAI proposing a higher ceiling
    for herself."""
    ...

def guardian_verify_owner_for_recovery(
    db: Session, *, claimed_owner_id: uuid.UUID, identity_assertion: "SovereignIdentityAssertion",
) -> bool:
    """The ONE door that must still open when everything above Guardian is untrusted.
    Delegates the actual cryptographic verification to V2-G's Sovereign Identity primitives
    -- Guardian does not invent its own auth scheme, it consumes one, matching this whole
    document's 'stay small, stay auditable, don't reinvent' stance. Replaces today's weak
    clear_kill_switch_for_recovery() ack (denylist+regex, found but not fixed tonight) with a
    real cryptographic check -- this closes that exact gap, in the right layer instead of a
    local patch."""
    ...
```

## 4. Relationship to `execution_envelopes`

**Decision: Guardian sits ABOVE `execution_envelopes`, as the ceiling `authorize_execution_scope()`
itself must respect — not a separate escalation path invoked only in exceptional situations.**

Reasoning: making Guardian an "only in emergencies" path would mean the normal, everyday
authorization flow never actually touches Guardian at all — which defeats the purpose of a
root-of-trust that's supposed to bound *every* grant, not just the dramatic ones. Concretely:
`authorize_execution_scope()`'s existing logic (never trusts the caller's own proposal, always
requires the caller's own explicit assertion) gets one more check added at the top: does the
requested scope fit within Guardian's currently-set `AuthorityCeiling` for this owner/domain?
If not, `authorize_execution_scope()` itself fails closed — Guardian is consulted on the
normal path, not bypassed by it. This is additive to the existing function (one more
precondition), not a rewrite, and does not touch #245's frozen candidate — it's a V2-only
extension point on top of code #245 doesn't itself modify.

## 5. Threat model: what if Guardian itself is compromised?

This is the most important failure mode and the brief correctly does not let it go unanswered.

- **Guardian must be small enough to audit completely** (§0) — this is the primary defense,
  not a secondary one. The attack surface of "a few hundred lines of mechanical DB
  transitions with no model calls" is categorically smaller than "a reasoning system." Every
  line should be reviewable by a human in one sitting, same bar as `kill_switch.py` tonight.
- **Guardian holds no data of its own** beyond authority/containment state — it is not a
  richer target than it needs to be. Vault contents, conversation history, and Sentinel
  detections live elsewhere; Guardian only holds ceilings and stop/grant epochs.
- **Guardian's own state changes require owner-identity verification for anything that
  RAISES a ceiling**, but a compromised Guardian LOWERING authority (spurious containment) is
  the fail-safe direction — `SECURITY FAILURE -> REDUCE AUTHORITY` means an attacker who
  compromises Guardian and can only make it *more* restrictive has achieved a denial-of-
  service, not a privilege escalation. The asymmetry is deliberate: raising a ceiling is hard
  and gated; lowering one is cheap and available even to a partially-untrusted caller,
  because the failure mode of "too restrictive" is recoverable (owner re-verifies identity,
  V2-H recovery flow) while "too permissive" may not be.
- **Guardian's code and its currently-active ceilings should be independently attestable**
  (a hash/signature the owner or an external verifier can check against a known-good
  baseline) — full design deferred to V2-J's implementation phase, but the requirement is
  recorded here so it isn't lost: system integrity checking (Guardian's own listed
  responsibility, §1) must include *Guardian checking itself*, not only MainAI's runtime.
- **What Guardian explicitly cannot protect against**: a compromised OS/hypervisor beneath
  everything, or physical device coercion of the owner. These are V2-G/V2-O concerns
  (device trust, sovereign identity) and should not be silently assumed solved by Guardian's
  existence — stated here so V2-J's threat model doesn't overclaim.
