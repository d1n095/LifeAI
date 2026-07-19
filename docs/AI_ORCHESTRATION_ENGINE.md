# AI Orchestration Engine — architecture only, no implementation

**Scope:** the authoritative deep-dive for the orchestration engine sketched at a summary
level in `docs/AI_PROVIDER_ARCHITECTURE.md` §6. That document defines the substrate this one
runs on top of (capability-based provider interfaces, the universal request/response schema,
the universal error taxonomy, provider manifests) — this document does not redefine any of
that, it consumes it. Cross-references `docs/MAINAI_ARCHITECTURE.md` §10 (Trust Engine) and
`docs/MEMORY_ARCHITECTURE.md` §6/§9/§11 (trust scoring, conflict resolution, human approval)
rather than duplicating them — the relationship between orchestration-time "which model should
handle this" and Trust Engine's "how much should we trust the answer we got" is the first
thing this document makes precise, because conflating them is the most common design error in
systems like this. **Nothing in this document is implemented. Pure architecture.**

---

## 1. What the orchestration engine is, and isn't

The orchestration engine decides **which candidate (provider, model) handles a request, and
by what execution strategy** (single attempt, retry, fallback, consensus, escalation). It
never decides what the *answer* is, and it never judges whether a *produced answer* is
well-grounded — that is `docs/MAINAI_ARCHITECTURE.md` §10's job. Two genuinely different
kinds of "confidence" exist in this system and must not be collapsed into one:

| | **Routing confidence** (this document, §5) | **Response confidence** (Trust Engine, `docs/MAINAI_ARCHITECTURE.md` §10) |
|---|---|---|
| Computed | Before/during candidate selection | After a response is produced |
| Input | Historical model performance, capability match, live health | Retrieval similarity, source authority, cross-source agreement |
| Answers | "Is this candidate likely to serve this request well?" | "Is this specific answer trustworthy?" |
| Consumed by | The router (§3), to rank/select candidates | The prompt (as an instruction) and the UI (as a confidence label) |

The orchestration engine **reads** Trust Engine's response confidence as one input to its
retry/escalation decisions (§8, §12) — a low-confidence answer can trigger the orchestration
engine to try again with a different candidate or escalate — but it does not compute response
confidence itself, and Trust Engine does not make routing decisions. Keeping this boundary
sharp is what keeps both systems independently reasoned-about instead of one 4,000-line
God-object.

---

## 2. High-level flow

```
Request
  │
  ▼
① Feature extraction  — capability needed, estimated size, tenant policy, latency budget,
                          stakes level (normal / high-stakes)
  │
  ▼
② Model scoring (§4)  — score every eligible candidate on quality/cost/latency/availability
  │
  ▼
③ Routing decision (§3) — rank candidates per the active policy; decide execution mode:
                            SINGLE | CONSENSUS (§10) | COLLABORATION_PIPELINE (§11)
  │
  ▼
④ Execution — dispatch to the chosen candidate(s), governed by retry (§8) and
                fallback (§9) policy on failure
  │
  ▼
⑤ Response confidence check — read Trust Engine's assessment of the result (or, for
                                 CONSENSUS, the agreement level across candidates, §10)
  │
  ├─ adequate ──────────────────────────────────► ⑥ Return response
  │
  └─ inadequate + high-stakes ──► ⑦ Human escalation (§12), reusing
                                     docs/MEMORY_ARCHITECTURE.md §11's approval queue
  │
  ▼ (always, regardless of path taken)
⑧ Observability (§13) — trace, log, and record metrics (§15) for every step above,
                          feeding §4's scoring loop and §16's health state
```

Step ⑧ is not a side effect bolted onto the end — it fires at every numbered step, and §4's
model scoring is itself fed by the accumulated output of step ⑧ over time (a closed loop:
routing decisions are informed by the recorded outcomes of past routing decisions).

---

## 3. Automatic model routing

Builds directly on `docs/AI_PROVIDER_ARCHITECTURE.md` §6's `CapabilityResolver`/`Router`
components — this section specifies exactly how a routing decision gets made, not a
replacement for that document's structure.

```python
class RequestFeatures(BaseModel):
    capability: Capability                 # from AI_PROVIDER_ARCHITECTURE.md §2
    estimated_input_tokens: int
    requires: set[Capability] = set()      # e.g. {TOOL_USE, VISION} for a multimodal request
    latency_budget_ms: int | None          # None = no explicit budget (batch/background work)
    stakes: Literal["normal", "high"]      # drives §10/§12 gating — see stakes determination below
    tenant_policy: ProviderPolicy          # AI_PROVIDER_ARCHITECTURE.md §8

class RoutingDecision(BaseModel):
    execution_mode: Literal["single", "consensus", "collaboration_pipeline"]
    candidates: list[ModelScore]           # ranked, §4 — first is primary, rest are the fallback chain (§9)
    reasoning: str                         # human-readable — WHY this ranking, for observability (§13)
```

**Stakes determination** is what decides whether consensus/escalation machinery is even
eligible for a request, and comes from three sources, any of which can raise a request from
`normal` to `high`: (a) an explicit tenant/admin policy flag on the capability or route (e.g.
"all `/api/admin/*`-triggered generations are high-stakes"), (b) a caller-supplied hint (a
user explicitly asking for a more careful answer), (c) `docs/MEMORY_ARCHITECTURE.md` §9's
conflict detection surfacing contradictory retrieved sources for this specific query — an
ambiguous knowledge base is itself a signal that the answer deserves more scrutiny, not just
a signal fed to Trust Engine after the fact.

**Routing policy** is a named, swappable strategy (matching
`docs/AI_PROVIDER_ARCHITECTURE.md` §6's strategy list) that turns a list of `ModelScore`s
into a `RoutingDecision`:

- `PriorityOrder` — today's behavior (`CHAT_FALLBACK_ORDER`), formalized as one strategy.
- `CostOptimized` (§6), `LatencyOptimized` (§7) — weight the composite score toward one
  factor.
- `QualityOptimized` — weight toward `quality_score` (§4) regardless of cost/latency, for
  tenants/routes that opt into it.
- `ComplianceConstrained` — filters by `DataResidencyPolicy`
  (`docs/AI_PROVIDER_ARCHITECTURE.md` §8) before any scoring-based ranking runs — a hard
  filter, never a soft preference, same rule as that document states.

---

## 4. Model scoring

The mechanism that makes routing decisions data-driven rather than a static hardcoded order —
**the single most load-bearing new concept in this document**, since §3, §6, §7, and §16 all
consume its output rather than each inventing their own candidate-ranking logic.

```python
class ModelScore(BaseModel):
    candidate: ModelCandidate            # (provider, model) — AI_PROVIDER_ARCHITECTURE.md §4/§5
    capability_match: float              # 0-1 — 1.0 = native support, partial for emulated/degraded
                                           # support (e.g. a model without native TOOL_USE that can
                                           # sometimes be prompted into a similar behavior — never
                                           # silently substituted without this being visibly < 1.0)
    quality_score: float                 # 0-1 — see "quality signal sources" below
    cost_score: float                    # 0-1, normalized inverse of estimated $/request
    latency_score: float                 # 0-1, normalized inverse of rolling p50 (§7)
    availability_score: float            # 0-1 — from health monitoring (§16), not circuit-breaker
                                           # boolean alone: DEGRADED candidates score lower than
                                           # HEALTHY ones without being fully excluded like an OPEN
                                           # circuit breaker would exclude them
    composite_score: float               # weighted sum; weights come from the active routing policy (§3)
    scored_at: datetime
```

**Quality signal sources, in order of how much weight each deserves as history accumulates:**

1. **Cold start (no history yet):** admin-configured static priors — an admin can declare "this
   model tends to be stronger at code" per capability, used only until enough live data exists.
2. **Trust Engine feedback (`docs/MAINAI_ARCHITECTURE.md` §10):** the rolling rate of
   `high`/`medium`/`low`/`none` response-confidence outcomes for a given (provider, model),
   aggregated per capability — a model whose answers are consistently well-grounded scores
   higher over time, entirely from data the system already produces for other reasons.
3. **Explicit human feedback:** thumbs up/down or an approval/rejection outcome
   (`docs/MEMORY_ARCHITECTURE.md` §11) tied to which candidate produced the reviewed content —
   the strongest signal, weighted accordingly, but the sparsest (most responses are never
   explicitly rated).

Quality scores are **recomputed on a schedule** (e.g. hourly rolling window), not per-request
— computing a live rolling average on every single chat turn would put a database aggregation
on the hot path of every request, exactly the anti-pattern §2's working-memory caching in
`docs/MEMORY_ARCHITECTURE.md` §2 was designed to avoid. A background job recomputes
`ModelScore` rows; request-time routing reads the already-materialized score, one cheap
lookup, matching that document's "compute once, read many" principle applied here to model
quality instead of memory trust.

---

## 5. Confidence estimation (routing-time)

Restated precisely, since §1's table draws the boundary but doesn't give the formula: routing
confidence for a candidate is a direct function of §4's `ModelScore` —

```
routing_confidence(candidate) = capability_match × quality_score × availability_score
```

**Not** cost or latency — those affect *ranking* (§3) among viable candidates, but a cheap,
fast candidate that's unlikely to produce an adequate answer is not "confidently routable" no
matter how cheap or fast it is. This keeps cost/latency optimization (§6/§7) and adequacy
estimation as separate concerns that combine at the ranking step, not conflated into one
number that would silently let a cost-optimized policy route to a candidate unlikely to
succeed just because it's cheap.

Routing confidence below a configurable floor removes a candidate from consideration entirely
(not just deprioritizes it) — this is what prevents, e.g., routing a vision request to a
model with `capability_match = 0` (no vision support at all) purely because it happens to be
the cheapest chat model.

---

## 6. Cost optimization

`CostOptimized` routing policy (§3), specified precisely:

- **Pre-call cost estimation:** `estimated_input_tokens` (§3's `RequestFeatures`) ×
  `app/providers/pricing.py`'s known per-1k-token rate for each candidate — computed before
  dispatch, not just recorded after (today's `estimate_cost()` only runs after a real call
  completes; routing needs a prediction beforehand to compare candidates).
- **Floor, not blind minimization:** cost optimization operates only among candidates whose
  `capability_match` and `quality_score` (§4) clear a minimum bar — the cheapest candidate
  that can't actually do the job is never selected just for being cheap; §5's routing-
  confidence floor is what enforces this, applied before cost ranking runs, not after.
- **Budget-aware rejection:** if a tenant has a `monthly_spend_cap_usd`
  (`docs/AI_PROVIDER_ARCHITECTURE.md` §8) and the estimated cost of even the cheapest
  qualifying candidate would breach the tenant's remaining budget, the orchestration engine
  fails closed — reject with a clear budget-exceeded message, never silently degrade to a
  lower-quality candidate without the tenant having opted into that trade-off explicitly via
  policy.

---

## 7. Latency optimization

`LatencyOptimized` routing policy (§3):

- **Per-request latency budget:** `RequestFeatures.latency_budget_ms` — an interactive chat
  request implies a tight budget (drives selection toward `ModelTier.FAST`,
  `docs/AI_PROVIDER_ARCHITECTURE.md` §4); a background document-summarization job has no
  meaningful budget and can route toward slower/cheaper/higher-quality tiers instead.
- **Rolling latency tracking:** p50/p95/p99 per (provider, model, capability), recomputed on
  the same schedule as §4's quality scores — feeds `latency_score` directly.
- **Hedged requests (target, high-value optimization for tail latency):** for requests with a
  tight latency budget, dispatch to the top-ranked candidate; if no response arrives within a
  configurable fraction of the latency budget (e.g. 50%), *also* dispatch to the second-ranked
  candidate in parallel, and use whichever responds first, cancelling the other. This trades a
  bounded amount of extra cost (only incurred on the tail — most requests never trigger the
  hedge) for materially better p99 latency, a well-established technique for exactly this
  problem shape (a small fraction of requests to any given provider are slow for reasons
  unrelated to the request itself — a transient blip, not a systemic issue that fallback/
  circuit-breaking should react to). Explicitly a target, not phase-1 — see §17.

---

## 8. Retry strategy

Extends `docs/AI_PROVIDER_ARCHITECTURE.md` §7's `ProviderErrorKind` taxonomy and §6's
`RetryPolicy` with the precise decision table that document left implicit:

| `ProviderErrorKind` | Retry same candidate? | Notes |
|---|---|---|
| `TIMEOUT` | Yes, once, then fallback (§9) | Transient — but a repeated timeout means fallback, not indefinite same-candidate retry |
| `RATE_LIMITED` | Yes, respecting `retry_after` | If `retry_after` exceeds the request's latency budget (§7), skip straight to fallback instead of waiting |
| `UNAVAILABLE` | No — straight to fallback | A 5xx/connection-refused is a candidate-level problem; retrying the same candidate rarely helps within a single request's lifetime |
| `AUTH` | No | A credential problem doesn't resolve by retrying — surface to admin (a health-monitoring alert, §16, not a per-request retry) |
| `QUOTA_EXCEEDED` | No — straight to fallback | Same reasoning as `AUTH` |
| `CONTENT_FILTERED` | **No retry, no fallback** | If one provider's safety system rejected the request, other providers are likely to as well — retrying/falling back here usually just spends more money to get the same (correct) rejection; surface the rejection directly |
| `INVALID_REQUEST` | No | This is a MainAI-side bug (malformed request), not a provider problem — retrying anywhere fails identically; this is a bug ticket, not an orchestration decision |
| `UNSUPPORTED_CAPABILITY` | No — straight to fallback | Expected outcome of `capability_match < 1.0` (§4) on a candidate that was included as a lower-scored fallback option |

**Retry budget, bounded per request:** a hard cap on total attempts (same-candidate retries +
fallback hops combined, e.g. 4) and total elapsed time (bounded by `latency_budget_ms` where
one is set) — prevents a single request from retry-storming across every configured provider
while a widespread outage is in progress, which would both waste money and add load to
already-struggling providers exactly when they need it least (this is the request-level
complement to §16's circuit breaker, which protects at the candidate level).

**Quality retry — a distinct concept from error-driven retry above:** when a response
*succeeds* (no `ProviderError`) but Trust Engine's response confidence (§1's table) comes back
`low`/`none` on a `high`-stakes request, that is not a retryable error in the §8 sense — it's
routed instead through §12's escalation path or, for a bounded, cheaper first attempt, a
**regeneration**: re-ask the same or next-ranked candidate with adjusted parameters (e.g.
`temperature=0` for determinism, or with additional retrieved context if `docs/
MEMORY_ARCHITECTURE.md` §9 flagged a conflict that a broader retrieval might resolve) before
falling back to full human escalation.

---

## 9. Fallback strategy

The `candidates` list in §3's `RoutingDecision` **is** the fallback chain — ordered by
composite score (§4), already filtered to routing-confidence-eligible candidates (§5). §8's
table determines which failures advance to the next candidate in this list versus stopping.
Formalizing what `docs/AI_PROVIDER_ARCHITECTURE.md` §6's `FallbackChain` states at a summary
level:

```
for candidate in routing_decision.candidates:
    if retry_budget_exhausted(): break
    result = execute(candidate, request)
    if result.success:
        return result
    if not should_advance(result.error.kind):   # §8's table — CONTENT_FILTERED/INVALID_REQUEST stop here
        raise result.error
    log_fallback(from=candidate, reason=result.error.kind)   # §13/§14
# every candidate exhausted
escalate_or_fail(request)   # §12
```

`log_fallback` firing is itself a health-monitoring signal (§16) — a candidate that's
frequently the "from" side of a fallback (as opposed to occasionally, which is normal and
expected) is a candidate whose `availability_score`/`quality_score` should already be
reflecting that in the next scoring cycle (§4), closing the loop between observed fallback
behavior and future routing decisions.

---

## 10. Consensus mode

**When invoked:** a `high`-stakes request (§3) under a routing policy that permits consensus
(an explicit tenant/route opt-in — never a silent default, because of the cost multiplier
below), or automatically when `docs/MEMORY_ARCHITECTURE.md` §9 flags unresolved retrieval
conflict for the query and the applicable policy says ambiguous-knowledge queries should get
consensus treatment rather than a single model's unsupported guess.

**Mechanism:**

```
1. DISPATCH — select N candidates (typically 3) from DIFFERENT underlying model families
   (never N keys/deployments of the SAME model — that doesn't diversify against a model
   family's shared blind spots, it just multiplies cost for correlated answers), dispatched
   in parallel, each independently unaware of the others' existence.

2. COLLECT — gather all N responses (with individual retry/fallback per §8/§9 applied to
   each leg independently — one leg failing doesn't abort the others).

3. AGGREGATE — the aggregation function depends on the response shape:
   - Short/factual answers: semantic-similarity clustering of the N answers; the majority
     cluster wins, and AGREEMENT RATIO (size of majority cluster / N) becomes the consensus
     confidence score — this is the generative-side counterpart to
     docs/MEMORY_ARCHITECTURE.md §9's retrieval-side conflict detection: disagreement among
     independently-generated answers is itself evidence about answer reliability, computed
     the same way disagreement among retrieved sources is.
   - Open-ended/long-form answers: an ARBITER call — a synthesis request (itself routed
     through this same engine, recursively, as an ordinary single-candidate CHAT capability
     call) that receives all N draft answers as input and produces either a synthesized
     answer or a selection among them, with justification.

4. RECORD — the individual per-candidate responses, the aggregation method used, and the
   resulting agreement/confidence are all persisted as provenance
   (docs/MEMORY_ARCHITECTURE.md §7's derived_from) if the result is promoted into memory, and
   as an observability trace (§13) regardless.
```

**Cost is N×**, explicitly — consensus is a deliberate trade of cost for reliability on
requests where being wrong is expensive, never a default execution mode. §6's cost-
optimization and this section are in direct tension by design; the routing policy (§3)
resolves that tension explicitly per tenant/route rather than the engine picking silently.

---

## 11. Multi-model collaboration

**Distinct from consensus** (§10 asks N models the *same* question independently, then
reconciles) — collaboration composes *different* models performing *different roles* toward
*one* answer, as a pipeline:

```python
class CollaborationStep(BaseModel):
    capability: Capability          # each step is an ordinary routed capability call (§2–§9) —
                                     # collaboration reuses the whole engine per step, not a new primitive
    input_from: list[str]           # references to prior steps' outputs, or the original request
    role: str                       # "extract", "classify", "draft", "critique", "synthesize" — descriptive,
                                     # feeds observability (§13), not a mechanism in itself

class CollaborationPipeline(BaseModel):
    steps: list[CollaborationStep]  # a DAG in general (parallel branches allowed), a simple
                                     # chain in the common case
```

**Canonical example — a generator/critic pipeline:** step 1 (`role="draft"`, CHAT capability)
produces a draft answer; step 2 (`role="critique"`, CHAT capability, a different/stronger
model) reviews the draft against the retrieved context and either approves or flags specific
issues; on flagged issues, step 1 re-runs with the critique as additional context (bounded to
one revision cycle to avoid unbounded back-and-forth). This is a concrete, buildable pattern
for materially improving answer quality on high-stakes requests at a fraction of consensus
mode's N× cost, since only the critique step (not the full draft) needs the stronger/pricier
model.

**Canonical example — a Life Library/Studio pipeline** (`docs/LIFE_LIBRARY_PLAN.md`): transcribe
(`TRANSCRIPTION` capability) → summarize (`CHAT`) → script-generate (`CHAT`) → synthesize
speech (`TTS`) for a LifeCast episode — four capability calls, four independently-routed
candidates (a transcription-specialist provider need not be the same vendor as the chat
provider), composed as one pipeline. This is the direct mechanism by which
`docs/AI_PROVIDER_ARCHITECTURE.md` §2's "same pattern, more capabilities" claim becomes a real
multi-step product feature rather than just four independent capability types that happen to
coexist.

Each step's failure handling (§8/§9) is local to that step — a pipeline-level policy decides
whether a failed step aborts the whole pipeline, skips (with a gap noted in the output), or
triggers escalation (§12), configured per pipeline definition rather than a single global rule.

---

## 12. Human escalation

**Trigger conditions**, each independently sufficient:

1. §9's fallback chain fully exhausted — no candidate could serve the request at all.
2. Response confidence (Trust Engine) is `low`/`none` on a `high`-stakes request, and §8's
   bounded regeneration attempt didn't improve it.
3. §10's consensus agreement ratio falls below a configured threshold (the models
   meaningfully disagreed and no arbiter synthesis could confidently resolve it).
4. A policy/compliance boundary requires explicit sign-off regardless of confidence (e.g. a
   `docs/MEMORY_ARCHITECTURE.md` §11 rule that some action classes always require approval,
   independent of how confident the system is).

**Mechanism — reuses infrastructure, does not invent a parallel one:** an escalation creates
a `pending` entry in the exact same approval queue `docs/MEMORY_ARCHITECTURE.md` §11 defines
for memory promotion — "this generated answer needs human review" and "this fact needs human
review before becoming organization memory" are the same kind of event (a low-confidence or
high-blast-radius artifact awaiting a human decision) and belong in one queue with one audit
trail (`docs/MEMORY_ARCHITECTURE.md` §14), not two parallel review systems a human has to
check separately.

**User-facing behavior while escalated:** never a silent hang.

- **Synchronous requests** (interactive chat): return immediately with an honest, generic
  "I'm not confident enough to answer this reliably — it's been flagged for review" (the same
  design instinct as `docs/MAINAI_ARCHITECTURE.md` §8's generic error responses — never expose
  internal routing/scoring detail to the end user, only to the reviewer).
- **Asynchronous requests** (document analysis, pipeline steps in §11): the pipeline pauses at
  the escalated step; downstream steps depending on it wait; the requester is notified the
  same way any other long-running async job completion is surfaced.

---

## 13. Observability (tracing)

Every numbered step in §2's flow emits a structured trace span, correlated by a single
`trace_id` per request, so a request's entire orchestration journey — which candidates were
scored, why the top one was chosen, whether it succeeded, what retries/fallbacks fired, whether
consensus or escalation engaged — is reconstructable after the fact from the trace alone, not
inferred from scattered log lines.

```python
class OrchestrationSpan(BaseModel):
    trace_id: UUID
    span_id: UUID
    parent_span_id: UUID | None       # nested spans for collaboration pipeline steps (§11) and
                                        # consensus legs (§10) — a tree, not a flat list
    step: Literal["feature_extraction", "scoring", "routing", "execution", "confidence_check",
                   "escalation"]
    candidate: ModelCandidate | None
    started_at: datetime
    ended_at: datetime | None
    outcome: Literal["success", "retried", "fell_back", "escalated", "failed"]
    metadata: dict                     # step-specific — routing reasoning (§3), agreement ratio (§10), etc.
```

This directly generalizes `docs/AI_PROVIDER_ARCHITECTURE.md` §3's `ChatResponse.provider`/
today's `providers_attempted` field (a flat list of names) into a full structured, nested
trace — `providers_attempted` becomes a derived summary view over a trace's spans, not a
separately-maintained field.

---

## 14. Logging

Structured, never free text, and deliberately privacy-conscious by default:

```python
class OrchestrationLogEntry(BaseModel):
    trace_id: UUID
    tenant_id: UUID | None
    capability: Capability
    candidate: ModelCandidate
    decision: Literal["routed", "retried", "fell_back", "consensus_dispatched", "escalated"]
    outcome: Literal["success", "error"]
    error_kind: ProviderErrorKind | None
    latency_ms: int
    cost_usd: Decimal | None
    timestamp: datetime
    # Deliberately NOT included by default: prompt content, response content — logging full
    # request/response bodies is an explicit, per-tenant opt-in for debugging (with its own
    # retention limit), never the default, matching docs/MAINAI_ARCHITECTURE.md §8's "never
    # log sensitive data implicitly" instinct and the consent/visibility boundaries
    # docs/MEMORY_ARCHITECTURE.md's SHARED-scope design treats just as seriously.
```

Every log entry doubles as an `EPISODIC` audit record
(`docs/MEMORY_ARCHITECTURE.md` §14) when it represents a decision with real consequence
(escalation, consensus dispatch, a fallback caused by a policy/budget rejection) — reusing that
document's "audit trail is a scoped episodic stream, not a bolted-on ninth system" principle
here rather than inventing separate orchestration-specific audit storage.

---

## 15. Metrics

A concrete taxonomy, not a vague "add monitoring" — each metric explicitly feeds either a
dashboard (human-facing) or §4's scoring loop (machine-facing), both listed:

| Metric | Type | Labels | Feeds |
|---|---|---|---|
| `orchestration_requests_total` | Counter | `capability, provider, model, outcome` | Dashboards; §4 quality/availability signal |
| `orchestration_retries_total` | Counter | `capability, provider, reason` | Dashboards; §16 health state |
| `orchestration_fallbacks_total` | Counter | `from_provider, to_provider, reason` | Dashboards; §4 availability_score |
| `orchestration_consensus_invocations_total` | Counter | `capability, agreement_bucket` | Dashboards; cost oversight (§6) |
| `orchestration_escalations_total` | Counter | `capability, reason` | Dashboards; §12 queue depth alerting |
| `orchestration_latency_seconds` | Histogram | `provider, model, capability` | §7 latency_score |
| `orchestration_cost_usd` | Histogram | `provider, model, capability` | §6 cost_score; tenant budget tracking |
| `orchestration_confidence_score` | Histogram | `capability, level` | §4 quality_score; product-level quality dashboards |
| `circuit_breaker_state` | Gauge | `provider, model` | §16; excluded directly from routing (§3) when `open` |
| `budget_remaining_usd` | Gauge | `tenant_id` | §6's budget-aware rejection |

Every metric here is a straightforward aggregation over §13's trace spans / §14's log entries
— there is no metric collection path independent of the tracing/logging already described;
metrics are a view, not a fourth data-collection mechanism.

---

## 16. Health monitoring

**Distinct from the circuit breaker** (`docs/AI_PROVIDER_ARCHITECTURE.md` §6), which is
*reactive* — it only reacts to failures that real traffic already produced. Health monitoring
is **proactive**: independent of real request volume, so a degrading provider is caught during
a quiet period instead of only being discovered when the next real user's request happens to
hit it.

```python
class HealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"     # elevated latency/error rate on recent synthetic or real checks,
                               # still serving — feeds a REDUCED (not zero) availability_score (§4)
    UNHEALTHY = "unhealthy"   # circuit breaker open, or synthetic checks failing outright
    UNKNOWN = "unknown"       # no recent data — treated conservatively (low availability_score,
                               # not assumed healthy by default)

class HealthCheck(BaseModel):
    candidate: ModelCandidate
    state: HealthState
    last_checked_at: datetime
    check_type: Literal["synthetic", "real_traffic_derived"]
    latency_ms: int | None
    detail: str | None
```

**Synthetic checks:** a lightweight, scheduled probe per registered candidate (a minimal
`embed()` call, or a provider's own status endpoint where one exists) run on a fixed interval
independent of real traffic — this is what lets §4's `availability_score` reflect current
reality even for a low-traffic candidate that real requests haven't exercised recently.

**Real-traffic-derived checks:** the same rolling success/latency data §4/§7 already compute
from actual requests, reused here rather than duplicated — synthetic and real-traffic signals
are combined (synthetic fills gaps when traffic is sparse; real traffic is weighted more
heavily when plentiful, since it's evidence about the exact conditions real users experience).

Health state changes are themselves observability events (§13/§14/§15) — a candidate flipping
`HEALTHY`→`DEGRADED` is exactly the kind of event that should alert an operator before it
becomes a wave of real failed requests, which is the entire point of proactive monitoring over
purely reactive circuit-breaking.

---

## 17. Migration path — additive, not a rewrite

| Phase | What changes | What doesn't |
|---|---|---|
| **0 (idag)** | `chat_with_fallback()` — a single hardcoded priority order, retry-once-per-candidate-then-next, `UsageLog` records successes only | — |
| **1** | Structured tracing (§13) and logging (§14) wrapped around today's existing fallback function — pure observability addition, zero behavior change; this alone makes every subsequent phase's data-driven decisions (§4, §7, §16) possible, since they all consume this phase's output | Routing behavior itself |
| **2** | `ModelScore`/scoring background job (§4), computed from phase 1's recorded outcomes plus Trust Engine's existing confidence output (`docs/MAINAI_ARCHITECTURE.md` §10, already computed today, just not yet fed back into routing) — routing still defaults to `PriorityOrder` until an operator opts a route into a different policy | Default runtime behavior |
| **3** | §8's full retry decision table + bounded retry budget; §16's synthetic health checks feeding `availability_score` | Fallback-chain shape (unchanged, just better-informed) |
| **4** | `CostOptimized`/`LatencyOptimized`/`QualityOptimized` routing policies (§6/§7) become selectable per tenant/route | Requests not opted in |
| **5** | Consensus mode (§10) and collaboration pipelines (§11) — the highest-complexity, highest-value phase, deferred until phases 1–4 provide the scoring/observability foundation both depend on (consensus's aggregation needs reliable agreement measurement; pipelines need per-step routing to already work) | Single-candidate requests (unaffected) |
| **6** | Human escalation (§12) wired to `docs/MEMORY_ARCHITECTURE.md` §11's approval queue — deferred until that queue infrastructure itself exists (that document's own phase 5) | — |
| **7** | Hedged requests (§7) — the most speculative optimization here, worth deferring until real p99 latency data (phase 1 onward) shows it's actually needed rather than guessed at | Everything built in phases 1–6 |

Each phase is independently shippable and a strict superset of what exists, consistent with
`docs/AI_PROVIDER_ARCHITECTURE.md` §11 and `docs/MEMORY_ARCHITECTURE.md` §15's migration
philosophy — nothing above is a placeholder, and the absence of any given phase today is
exactly why `docs/AI_PROVIDER_ARCHITECTURE.md`'s own orchestration section says "designat,
inte byggt."
