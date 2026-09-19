# MainAI V2 — Privacy Boundary Engine (Stage V2-C)

**Status:** design-only. Does not modify PR #245 / candidate SHA `818dfb732da47901eb5ae06ffdd9c829fe00c4c5`.
Assumes `docs/mainai_v2/MAINAI_V2_ARCHITECTURE_MAP.md` (V2-A) as shared vocabulary.

## 0. Relationship to the existing `app.egress_policy` — extends it, does not replace it

Checked the actual code (`backend/app/egress_policy/service.py`) before designing anything.
It already implements real pieces of what this document needs: a default-deny
`enforce_egress_policy()` gate, a durable `ProviderDisclosureEvent` audit row for *every* call
(allowed or denied), secret-pattern redaction, hard-refuse markers (`NEVER_EGRESS:`, private
key blocks), and — critically — the exact doctrine this document also needs:
`RETRIEVAL AUTHORITY != EGRESS AUTHORITY`, stated as being the same invariant family as
`PROPOSED_SCOPE != AUTHORIZED_SCOPE`. It is deliberately narrow today: wired only into the
Safe Planner boundary, explicitly not yet into `app/routers/chat.py` or RAG embedding calls
(its own docstring says so).

**Decision: the Privacy Boundary Engine is `app.egress_policy` generalized to cover every
outbound signal (not just provider-planning calls) and given two capability layers it does
not have yet — semantic minimization/generalization, and structural telemetry-mode
enforcement — not a separate, parallel gate.** The existing `ProviderDisclosureEvent` audit
table and default-deny/fail-closed posture are reused as-is; this document adds the stages
that sit *before* today's redaction step.

## 1. Pipeline stages — schema

```python
# Illustrative, non-imported prototype code -- not wired into any runtime path yet.
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import UUID

class TelemetryMode(str, Enum):
    OFF = "OFF"
    MINIMAL = "MINIMAL"
    LEARNING = "LEARNING"
    RESEARCH_OPT_IN = "RESEARCH_OPT_IN"

class DataSensitivity(str, Enum):
    PUBLIC = "public"                # already-public facts, e.g. library hours
    INTERNAL = "internal"            # ordinary local content, not sensitive
    PERSONAL = "personal"            # names, relationships, personal preferences
    SENSITIVE = "sensitive"          # finance amounts, health, legal specifics
    SECRET = "secret"                # vault-classed, never crosses the boundary at all

@dataclass(frozen=True)
class RawLocalSignal:
    """Stage 0 input -- exactly what MainAI/a department observed locally."""
    owner_id: UUID
    domain: str                      # e.g. "debt_resolution", "malware_response"
    raw_content: Any                 # the actual private content -- text, structured data
    sensitivity: DataSensitivity
    source: str                      # "conversation" | "document" | "sentinel_event" | ...

@dataclass(frozen=True)
class ClassifiedSignal:
    """Stage 1 output: what KIND of thing this is, not what it says."""
    domain: str
    skill: str | None                # e.g. "rule_application", "malware_signature_match"
    outcome_class: str | None        # e.g. "resolved_locally", "knowledge_gap", "failed"
    sensitivity: DataSensitivity
    raw_ref: "RawLocalSignal"        # kept LOCAL ONLY -- never part of the egress payload itself

@dataclass(frozen=True)
class MinimizedSignal:
    """Stage 2: reduced to the smallest fact set that still lets central learn something
    about the SOFTWARE, with identifying specifics already gone."""
    domain: str
    skill: str | None
    outcome_class: str | None
    agent_version: str
    latency_bucket: str | None       # coarse-bucketed, never exact timing that could fingerprint
    error_category: str | None       # a closed vocabulary, never a raw error string

@dataclass(frozen=True)
class GeneralizedSignal:
    """Stage 3: MinimizedSignal is already mostly there by construction (it never carried
    specifics) -- this stage exists for cases where domain/skill values THEMSELVES could be
    identifying at small scale (e.g. a rare legal specialty only one owner uses) and need
    coarsening to a broader category, plus k-anonymity-style suppression (see §4)."""
    generalized_domain: str
    generalized_skill: str | None
    outcome_class: str | None
    agent_version: str
    suppressed: bool                 # True if even the generalized form was withheld (§4)

@dataclass(frozen=True)
class PrivacyPolicyDecision:
    """Stage 4-5: policy + local approval, mirrors egress_policy's own allow/deny + reason."""
    allowed: bool
    telemetry_mode_applied: TelemetryMode
    reason: str
    requires_explicit_owner_opt_in: bool

@dataclass(frozen=True)
class EgressableSignal:
    """Stage 6 output -- the ONLY thing that may ever leave the device for this signal."""
    payload: dict[str, Any]          # exactly the GeneralizedSignal fields, nothing else
    disclosure_event_id: UUID        # reuses ProviderDisclosureEvent-shaped audit row
```

Every stage is a pure function of the previous stage's output — later stages structurally
cannot see `RawLocalSignal.raw_content` (it isn't threaded through the dataclasses past
`ClassifiedSignal.raw_ref`, and `raw_ref` itself is dropped before `MinimizedSignal` exists).
This makes "the pipeline forgot to strip something" require an actual code change to
`ClassifiedSignal` to even become *possible* to leak, not just a review-time habit.

## 2. Five worked examples (founder gave one; four more here)

| Domain | Local (never leaves) | Central receives |
|---|---|---|
| Debt (founder's own example) | Specific debt, company name, amount, full conversation | `domain=debt_resolution, skill=rule_application, outcome_class=resolved_locally, agent_version=X` |
| **Legal question** | "My landlord Andersson is trying to evict me over the balcony plants, lease clause 7.2" | `domain=legal.rental_law, skill=clause_interpretation, outcome_class=knowledge_gap, agent_version=X` — no landlord name, no clause text, no address |
| **Health question** | "My father's cholesterol is 6.8, on statins, worried about the new dosage" | `domain=health.medication_interaction, skill=dosage_lookup, outcome_class=resolved_locally, agent_version=X` — no relation, no values, no drug name (drug name generalized to `medication_interaction` category unless the owner explicitly opts into a more specific research-sharing mode) |
| **Security incident** | Real malware hash, the specific file path, the specific process tree on this owner's machine | `domain=security.malware_response, skill=signature_match, outcome_class=contained_locally, agent_version=X, threat_family=<already-public malware family name>` — the *pattern* is shareable (that's genuinely useful to Sentinel's central threat-intel improvement), the *instance* (this owner's specific file, this owner's specific process tree) is not |
| **Relationship/family matter** | "My sister and I are fighting about our mother's estate, she wants to sell the house" | `domain=personal.family_estate, skill=none (no rule engine involved, pure listening), outcome_class=n/a` — in most modes this domain is HIGH-sensitivity enough that even the generalized form is suppressed by default (§4); MainAI's help here needs zero central signal to function |

## 3. Telemetry modes — structural enforcement, not a settings flag

The founder's own bar: "LifeAI central systems should ideally be technically incapable of
requesting raw private user memory." A settings flag MainAI could theoretically ignore does
not meet that bar. Design requirement, in order of how hard the guarantee is:

1. **`OFF` must be enforced below the classify stage, not at the egress stage.** If
   `TelemetryMode == OFF`, the pipeline must not even construct a `ClassifiedSignal` for
   learning purposes — the function that would do so returns immediately. This is stronger
   than "the egress step refuses to send": it means there is no in-memory object anywhere in
   the process, however briefly, that represents "this signal, prepared to leave." Matches
   `enforce_egress_policy()`'s own existing "structurally stateless" discipline (no cache to
   accidentally serve from) applied one layer earlier.
2. **`OFF` must have zero code paths, not a config value checked at runtime, for the specific
   category the founder named explicitly**: advertising IDs, cross-device fingerprinting,
   hidden analytics. These are not "class of signal minimized like everything else" — they
   should not exist as functions in the codebase at all, so there's nothing to misconfigure.
   This is the one place this document recommends *removing capability from the code*, not
   gating it.
3. Every mode transition (`OFF -> MINIMAL`, etc.) is itself a durable, audited event — same
   pattern as `ProviderDisclosureEvent` — with the owner's own explicit action as the only
   valid cause. MainAI proposing a mode change to herself is not sufficient (matches
   `MODEL OUTPUT != AUTHORITY`); an actual owner UI action is required, surfaced through a
   `VISIBLE_SURFACE` (V2-A §1), never inferred from conversation tone.
4. `LEARNING` and `RESEARCH_OPT_IN` differ only in scope: `LEARNING` sends the
   `GeneralizedSignal` shape above; `RESEARCH_OPT_IN` may include coarser-but-still-real
   content (e.g. the specific drug name in the health example) ONLY for domains the owner has
   separately, explicitly opted into per-domain — never a single blanket toggle that silently
   widens every domain at once.

## 4. Suppression / small-cohort protection

`GeneralizedSignal.suppressed` exists because generalization alone doesn't prevent
re-identification when a category is rare — one owner with a legal specialty nobody else in
the cohort has is still identifiable even after "generalizing" to that specialty's name.
Minimum viable rule for V2 (full differential-privacy tooling is a V2-J implementation-phase
decision, not baked in here): a signal is suppressed entirely (sent as
`outcome_class` + `agent_version` only, domain/skill dropped) if the responsible aggregation
service cannot confirm the domain/skill combination currently has at least a minimum cohort
size (a concrete number to be set at implementation time, not invented here) among opted-in
owners. Fail-closed: if cohort size is unknown (aggregation service unreachable, stale count),
treat as below threshold and suppress — same `UNKNOWN SAFETY STATE != SAFE` doctrine already
proven in `evaluate_startup_readiness()` tonight, applied to privacy instead of authority.

## 5. Failure mode: what if classify/minimize itself fails?

**Fails CLOSED — the pipeline denies egress on any stage exception, exactly like
`enforce_egress_policy()`'s existing posture (a malformed request is denied outright, not
partially sent).** A bug in the classify or minimize stage must never result in the
`RawLocalSignal`'s actual content falling through to the egress step by default/exception
handling accident — there is no "if minimization fails, just send the classified version
instead" fallback anywhere in this design, because that fallback would itself be a slow
privacy leak triggered by any bug in the minimizer. Concretely: any unhandled exception in
stages 1–4 aborts the whole signal (it is simply never sent, not sent-unminimized), and is
itself logged locally (never centrally) as an `owner-visible` "MainAI tried to learn from
this and couldn't, nothing was sent" note — visible to the owner via a `VISIBLE_SURFACE` if
they ask "what do you remember", never silently swallowed either.
