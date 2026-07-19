# AI Provider Architecture — MainAI as a provider-agnostic AI substrate

**Scope:** this is the deep-dive design for `docs/MAINAI_ARCHITECTURE.md` §7 (AI
orchestration) and §1's principle 3 ("leverantörsoberoende genom abstraktion, inte genom
villkorssatser"). That document gives the summary; this one gives the actual interface
contracts, the normalization mechanism, and the enterprise-scale orchestration design —
written so that a new provider (OpenAI, Anthropic, Gemini, a local model, or something that
doesn't exist yet) is a **pure addition**: one adapter file, one manifest, zero changes to
`routers/`, `rag/`, or any application code. Pure architecture — nothing here is implemented
yet beyond what §"Idag" explicitly says.

## Design law

> **The application layer knows capabilities. It never knows providers.**

Every line of this document exists to make that one sentence literally, mechanically true —
not an aspiration enforced by code review, but a property the type system and the
orchestration layer make hard to violate. A router, a RAG function, a Trust Engine
signal-computation — none of them may contain the string `"openai"`, `"anthropic"`, or any
other provider identifier as a branch condition. The only places a provider name is allowed to
appear in application-facing code are: (a) an **adapter**, (b) a **manifest**, (c) **admin
configuration** (a human choosing which provider to use for a role — data, not logic), and
(d) **logs/audit records** (a fact about what happened, not a decision point).

This is the concrete mechanism behind `docs/MAINAI_ARCHITECTURE.md`'s framing of MainAI as an
eventual substrate that can absorb any AI provider — including a future first-party MainAI
model — without the rest of the system caring which one answered.

---

## 1. Why "one interface with chat/embed" is not enough at this scale

**Idag:** `LLMProvider` (`backend/app/providers/base.py`) is a single ABC with three methods:
`chat()`, `embed()`, `is_configured()`. This has served the MVP well (six real providers, a
working fallback chain, real cost logging) and nothing in this document invalidates it as a
starting point — but it has four properties that don't hold at enterprise scale, each of
which this document fixes:

| Limitation today | Consequence at scale |
|---|---|
| One flat interface, not capability-scoped | A provider that only does embeddings (e.g. a dedicated embedding model) still has to implement a `chat()` stub, and the registry can't ask "which providers support vision?" without trying and catching |
| No universal request/response schema — `Message`/`ChatResult` are already "universal enough" for text-only chat, but there's no `ToolCallPart`, `ImagePart`, streaming event, or structured-output schema | Adding tool-use or vision support means widening the shared dataclasses AND touching every adapter's translation logic simultaneously — not additive |
| No universal error taxonomy — `except Exception` in `chat_with_fallback()` treats every failure identically | Can't distinguish "this key is out of quota, try the next provider" from "this request violated content policy, retrying anywhere will fail the same way" from "transient network blip, retry the SAME provider after a short delay" — all three need different orchestration responses |
| Provider registration is a hardcoded dict (`_PROVIDERS` in `registry.py`) | Adding a provider means editing a file every other provider's code also lives in — not a plug-and-play boundary, just a shorter list to edit |

Sections 2–9 below fix these in order. Section 10 gives the concrete migration path from
today's code to this design — additive, not a rewrite.

---

## 2. Capability-based interfaces, not a provider-based interface

A provider is described by **which capabilities it implements**, not by a name. The
orchestration layer, the registry, and every application call site operate on capabilities.

```python
class Capability(str, Enum):
    CHAT = "chat"
    CHAT_STREAMING = "chat_streaming"
    EMBEDDING = "embedding"
    VISION = "vision"                 # image content parts in chat messages
    TOOL_USE = "tool_use"             # function/tool calling
    STRUCTURED_OUTPUT = "structured_output"   # JSON-schema-constrained responses
    TRANSCRIPTION = "transcription"   # speech-to-text (docs/LIFE_LIBRARY_PLAN.md §7)
    TTS = "tts"                       # text-to-speech
    DIARIZATION = "diarization"       # speaker identification


class ChatCapable(Protocol):
    async def chat(self, request: ChatRequest) -> ChatResponse: ...

class ChatStreamingCapable(Protocol):
    async def chat_stream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]: ...

class EmbeddingCapable(Protocol):
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse: ...

class TranscriptionCapable(Protocol):
    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse: ...

# ... one Protocol per capability, same shape. TTSCapable/DiarizationCapable follow identically
# when docs/LIFE_LIBRARY_PLAN.md's modalities are actually built (§10 below).
```

**A provider adapter implements only the Protocols it actually supports** — an
embedding-only provider (e.g. a dedicated embedding-model vendor, or a future in-house
embedding model) implements `EmbeddingCapable` and nothing else. There is no stub `chat()`
method raising `NotImplementedError` anywhere — Python's structural typing (Protocols) means
"does this adapter support chat" is answered by `isinstance(adapter, ChatCapable)` /
`hasattr`-style capability checks at registration time, not by a base class forcing every
method to exist.

This directly enables `docs/MAINAI_ARCHITECTURE.md` §7's "fler modaliteter, samma mönster"
target: transcription/vision/TTS providers are not a special case requiring new orchestration
concepts — they're the same pattern with a different Protocol.

---

## 3. The normalization layer — universal request/response schema

This is the single most important piece for "no provider-specific logic leaks into the
application." Every adapter's job is to translate **into and out of** these types — the
translation logic (and *only* the translation logic) is where a provider's wire format is
allowed to exist in code.

```python
class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

# Content is a list of typed parts, not a bare string — this is what makes vision/tool-use
# additive later instead of a breaking change to every adapter today.
class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str

class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    source: ImageSource   # url | base64 — never a provider-specific image encoding

class ToolCallPart(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str
    tool_name: str
    arguments: dict

class ToolResultPart(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    content: str

ContentPart = TextPart | ImagePart | ToolCallPart | ToolResultPart

class UniversalMessage(BaseModel):
    role: Role
    content: list[ContentPart]

class ToolSpec(BaseModel):
    name: str
    description: str
    parameters_schema: dict   # JSON Schema — provider-agnostic; adapter translates to
                               # OpenAI's "function" shape, Anthropic's "tool" shape, etc.

class ChatRequest(BaseModel):
    messages: list[UniversalMessage]
    model: str                        # a LOGICAL model id — see §4, never a raw provider string
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[ToolSpec] | None = None
    tool_choice: Literal["auto", "required", "none"] | str | None = None
    response_schema: dict | None = None   # JSON Schema for STRUCTURED_OUTPUT
    stop_sequences: list[str] | None = None
    metadata: RequestMetadata          # tenant_id, request_id, trace_id — routing/audit only,
                                        # an adapter must never forward this to the provider verbatim

class FinishReason(str, Enum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALL = "tool_call"
    CONTENT_FILTERED = "content_filtered"
    ERROR = "error"

class UsageInfo(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int = 0            # prompt-caching credit, where a provider reports it —
                                       # normalized to one field instead of a provider-specific key

class ChatResponse(BaseModel):
    content: list[ContentPart]
    finish_reason: FinishReason
    usage: UsageInfo
    provider: str                     # fact, for audit/logging — never a branch condition upstream
    model: str
    raw: dict = Field(exclude=True)   # escape hatch for debugging ONLY — the application layer
                                       # must never read this field; only logging middleware may
```

**Streaming events** follow the same principle — one universal event union, not a raw passthrough
of each provider's SSE/chunk format:

```python
class ChatStreamEvent(BaseModel):
    # Discriminated union: TextDelta | ToolCallDelta | FinishEvent | UsageEvent
    ...
```

**Why `raw: dict` exists and why it's dangerous by design:** every provider occasionally
returns information the universal schema doesn't model yet (a new OpenAI field, an
Anthropic-specific citation format). Keeping the raw payload prevents silent data loss and
gives debugging a way to see exactly what the provider said — but it is `exclude=True` from
serialization and, by convention enforced in code review (and, at scale, by a lint rule
checking for `.raw` access outside `app/providers/` and `app/observability/`), is never read
by orchestration or application code. If something in `raw` is needed regularly, that's a
signal the universal schema is missing a field — fix the schema, not the call site.

---

## 4. Logical models, not provider model strings

`ChatRequest.model` is never `"gpt-4o-mini"` or `"claude-sonnet-5"` directly from application
code — it's a **logical model identifier** resolved through the registry (§5), the same way
`ProviderConfig`'s role-based resolution (`"chat"`/`"embedding"`) already works today, extended
with model-tier semantics:

```python
class ModelTier(str, Enum):
    FAST = "fast"          # cheap/low-latency — e.g. gpt-4o-mini, claude-haiku, gemini-flash
    BALANCED = "balanced"  # today's default chat model
    ADVANCED = "advanced"  # highest-capability — e.g. claude-opus, gpt-4o, gemini-pro
    LOCAL = "local"         # explicit request for on-device/on-prem inference

class ModelDescriptor(BaseModel):
    logical_id: str                # "chat.balanced", "embedding.default", "vision.advanced"
    provider_model_string: str     # the ACTUAL string sent on the wire — "gpt-4o-mini" etc.
    capabilities: set[Capability]
    context_window: int
    tier: ModelTier
```

Application code (chat.py, RAG, Trust Engine's future multi-provider consensus, §"AI
orchestration" in `docs/MAINAI_ARCHITECTURE.md` §7) asks for `chat.balanced` or
`embedding.default`, never `"gpt-4o-mini"`. This is what makes §6's routing engine able to
freely substitute providers (including a local model) for the same logical request without
any call site knowing or caring.

---

## 5. Provider manifests — the plug-and-play mechanism

A provider is **registered by declaring a manifest**, not by being hardcoded into a shared
dict every other provider also lives in (§1's fourth limitation).

```python
class CredentialSchema(BaseModel):
    required_fields: list[str]        # e.g. ["api_key"], or ["base_url"] for a local runner
    validation: Callable[[dict], bool] | None = None

class DataResidencyPolicy(BaseModel):
    regions: set[str]                  # e.g. {"eu", "us"} — which regions this provider's
                                        # infrastructure is known to process data in
    on_premises: bool = False          # true for local/self-hosted adapters

class ProviderManifest(BaseModel):
    id: str                            # "openai", "anthropic", "gemini", "acme-llm-2027", "ollama"
    display_name: str
    capabilities: set[Capability]
    credential_schema: CredentialSchema
    model_catalog: list[ModelDescriptor]
    adapter_factory: Callable[[dict], object]   # dict = validated credentials -> adapter instance
    data_residency: DataResidencyPolicy
    conformance_suite: str              # see §9 — must pass before the registry trusts this manifest
```

**Registering a new provider is: write one adapter module + one manifest + pass the
conformance suite.** Nothing else changes. The registry (`ProviderRegistry`, replacing
today's flat `_PROVIDERS` dict) discovers manifests via an entry-point/plugin mechanism
(Python's `importlib.metadata.entry_points`, the same mechanism pytest/Django plugins use) so
that a manifest can even ship as a **separate installable package** — a third party (or a
future MainAI marketplace) can publish a provider adapter without ever touching this repo.

```python
class ProviderRegistry:
    def register(self, manifest: ProviderManifest) -> None:
        """Validates credential_schema against configured secrets, runs the conformance
        suite (§9) in a sandboxed mode, and only then makes the provider routable. A
        provider that fails conformance is registered but marked unavailable — visible in
        admin, never silently used."""

    def capable_of(self, capability: Capability, tier: ModelTier | None = None) -> list[ProviderManifest]:
        """The single function the routing engine (§6) queries — 'which currently-healthy,
        currently-configured providers can do X' — never a hardcoded provider list."""
```

---

## 6. Orchestration engine

**Se `docs/AI_ORCHESTRATION_ENGINE.md` för den fullständiga designen** — modellpoängsättning,
kostnads-/latensoptimering, en exakt beslutstabell för retry kontra fallback per
`ProviderErrorKind`, konsensusläge, multi-modell-samarbete (pipelines), eskalering till
människa (återanvänder `docs/MEMORY_ARCHITECTURE.md` §11:s godkännandekö), observability
(spårning/loggning/mätvärden) och proaktiv hälsoövervakning skild från circuit breakern. Det
dokumentet skiljer också explicit "routing confidence" (den här motorns egen bedömning av
vilken kandidat som ska hantera en förfrågan) från "response confidence" (Trust Engine,
`docs/MAINAI_ARCHITECTURE.md` §10, som bedömer om ett redan producerat svar går att lita på)
— två olika saker som annars lätt blandas ihop. Det här avsnittet är sammanfattningen; det
andra dokumentet är sanningskällan.

Where routing decisions actually get made — and the only place provider **selection** logic
lives (never inside a router, never inside RAG code).

### 6.1 Components

```
CapabilityResolver → Router → CircuitBreaker (per provider+model) → RetryPolicy → UsageRecorder
```

- **`CapabilityResolver`**: given (capability, tenant, logical model tier), produces the
  ordered candidate list — reads tenant policy (§8), admin `ProviderConfig` (today's
  mechanism, extended with tenant scoping), and the registry's `capable_of()`.
- **`Router`**: a pluggable **strategy**, not a single hardcoded order:
  - `PriorityOrderStrategy` — today's behavior (`CHAT_FALLBACK_ORDER`), formalized as one
    strategy among several rather than the only option.
  - `CostOptimizedStrategy` — picks the cheapest candidate first among those meeting a
    minimum capability/quality bar, using live `UsageLog` pricing data (§7's closed loop).
  - `LatencyOptimizedStrategy` — picks based on rolling p50/p95 latency per (provider,
    model), fed by `UsageRecorder`.
  - `LoadBalancedStrategy` — weighted round-robin across multiple keys/providers for the same
    capability, so no single key/provider absorbs 100% of traffic (also a rate-limit
    mitigation).
  - `ComplianceConstrainedStrategy` — filters candidates by `DataResidencyPolicy` before any
    other strategy runs, for tenants with a data-residency requirement (§8).
  - `LocalPreferredStrategy` — prefers `ModelTier.LOCAL` candidates first, falling back to
    cloud providers only on local unavailability — the literal mechanism behind "eventually
    replace every external AI": flip the default strategy, zero application code changes.
- **`CircuitBreaker`** (per provider+model pair): opens after N consecutive failures within a
  rolling window; while open, that candidate is skipped **without being attempted** (fail
  fast, not a slow timeout on every request while a provider is down); half-open after a
  cooldown period probes with a single request before fully closing again. **Not built
  today** — today's fallback chain retries every candidate on every request regardless of
  recent history, which is correct at MVP traffic levels and wasteful/slow at scale.
- **`RetryPolicy`**: exponential backoff, bounded attempts, and — critically — **only acts on
  `ProviderError.retryable == True`** (§7). A `CONTENT_FILTERED` error is never retried
  anywhere (retrying won't change the outcome); a `RATE_LIMITED` error respects
  `retry_after` if the provider supplied one.
- **`UsageRecorder`**: every attempt — success or failure — is recorded (extends today's
  `UsageLog`, which only records successes) with `provider`, `model`, `capability`, `latency`,
  `error_kind | null`, `cost`. This feeds `CostOptimizedStrategy`/`LatencyOptimizedStrategy`
  above (a closed loop: routing decisions are informed by the outcomes of past routing
  decisions) and the Trust Engine's future `source_authority` signal
  (`docs/MAINAI_ARCHITECTURE.md` §10).

### 6.2 What a call site actually does

```python
# app/routers/chat.py, target shape — compare to today's chat_with_fallback() call
response = await orchestrator.execute(
    capability=Capability.CHAT,
    request=ChatRequest(messages=..., model="chat.balanced", metadata=RequestMetadata(tenant_id=..., ...)),
    strategy=tenant_policy.routing_strategy,   # tenant-configurable, defaults to PriorityOrder
)
```

No provider name anywhere in this call. `response.provider`/`response.model` are available
afterward for logging/UI (`providers_attempted`, today's field, generalizes directly), never
for a decision.

---

## 7. Universal error taxonomy

The mechanism that lets `RetryPolicy`/`CircuitBreaker`/fallback logic make correct decisions
**without knowing which provider failed or why in provider-specific terms**.

```python
class ProviderErrorKind(str, Enum):
    AUTH = "auth"                          # invalid/expired/missing credential
    RATE_LIMITED = "rate_limited"           # 429-equivalent — retryable, often with retry_after
    QUOTA_EXCEEDED = "quota_exceeded"       # billing/quota exhausted — NOT retryable on this provider
    INVALID_REQUEST = "invalid_request"     # malformed request — a MainAI bug, not retryable anywhere
    CONTENT_FILTERED = "content_filtered"   # provider's safety system rejected the request/response
    TIMEOUT = "timeout"                     # retryable, same or different provider
    UNAVAILABLE = "unavailable"             # 5xx / connection refused — retryable, prefer next provider
    UNSUPPORTED_CAPABILITY = "unsupported_capability"  # asked for tool_use on a provider that can't
    UNKNOWN = "unknown"                     # adapter genuinely could not classify — logged loudly,
                                             # never silently treated as any of the above

class ProviderError(Exception):
    kind: ProviderErrorKind
    provider: str
    retryable: bool
    retry_after: float | None
    original: Exception            # the raw httpx/SDK exception — kept for logs, never inspected
                                    # by orchestration decision logic
```

**Every adapter's `except` block maps the provider's actual error shape into this taxonomy.**
This is where OpenAI's `{"error": {"type": "insufficient_quota", ...}}`, Anthropic's
`{"type": "error", "error": {"type": "rate_limit_error", ...}}`, and a raw `httpx.ConnectError`
from a local Ollama instance all become the same three or four `ProviderErrorKind` values the
orchestration engine actually reasons about. **This is the single concrete artifact that makes
"no provider-specific logic leaks into the application" verifiable, not just a stated
principle** — you can grep the codebase outside `app/providers/*_provider.py` for any
provider-specific string/status-code check and there should be zero matches; every decision
past the adapter boundary is a `ProviderErrorKind` switch, not a `"insufficient_quota" in str(exc)`
check.

---

## 8. Enterprise / multi-tenant concerns

Builds directly on `docs/MAINAI_ARCHITECTURE.md` §4's `Organization` target model and §9's
resource-level permission target — provider access is itself a **permission**, not a global
setting.

```python
class ProviderPolicy(BaseModel):
    organization_id: UUID
    allowed_providers: set[str] | None      # None = all registered providers allowed
    denied_providers: set[str] = set()      # explicit deny wins over allow
    routing_strategy: RouterStrategyName = "priority_order"
    data_residency_requirement: DataResidencyPolicy | None = None
    monthly_spend_cap_usd: Decimal | None
    credential_overrides: dict[str, CredentialRef]  # per-org API keys, not just one global key —
                                                       # a tenant can bring their own OpenAI key
```

- **Credential isolation**: per-organization credentials stored via a secrets-management
  layer (not a plaintext DB column — this repo's existing convention of "hemligheter aldrig i
  databasen i klartext," `docs/MAINAI_ARCHITECTURE.md` §8, extends directly to tenant-supplied
  provider keys), referenced by an opaque `CredentialRef`, resolved only at the moment the
  adapter is instantiated for a request.
- **Compliance/data-residency routing**: `ComplianceConstrainedStrategy` (§6) filters out any
  provider whose `DataResidencyPolicy` doesn't satisfy the tenant's requirement **before** any
  cost/latency optimization runs — compliance is a hard filter, never a soft preference.
- **Spend caps**: `UsageRecorder` (§6) checks cumulative monthly spend against
  `monthly_spend_cap_usd` before dispatching a request when a cap is set — fails closed (reject
  the request with a clear message) rather than silently exceeding a tenant's configured
  budget.
- **Audit**: every provider call already extends `AuditLog`
  (`docs/MAINAI_ARCHITECTURE.md` §4) with `organization_id`, `capability`, `provider`, `model`,
  `outcome`, `cost` — the same append-only pattern already used for account events, not a new
  logging mechanism.

---

## 9. Conformance test suite — plug-and-play, enforced not just claimed

"Every provider must be plug-and-play" is a testable property, not a design intention. A
shared, provider-agnostic test suite runs against **any** adapter before the registry (§5)
marks it routable:

```python
class ProviderConformanceTests:
    """One suite, parameterized over any adapter claiming a given capability. Run against a
    real sandbox/test credential where available, and against INJECTED FAULTS (mocked 401,
    429, 500, timeout, malformed JSON) to verify error-mapping correctness — the fault
    injection tests are what actually prove §7's error taxonomy mapping is correct, not just
    that the happy path works."""

    async def test_chat_returns_well_formed_response(self, adapter: ChatCapable): ...
    async def test_chat_maps_auth_failure_to_AUTH(self, adapter: ChatCapable): ...
    async def test_chat_maps_429_to_RATE_LIMITED_with_retry_after(self, adapter: ChatCapable): ...
    async def test_chat_maps_5xx_to_UNAVAILABLE(self, adapter: ChatCapable): ...
    async def test_chat_maps_connection_error_to_UNAVAILABLE(self, adapter: ChatCapable): ...
    async def test_usage_tokens_are_non_negative(self, adapter: ChatCapable): ...
    async def test_embed_returns_declared_dimension(self, adapter: EmbeddingCapable): ...
    async def test_streaming_events_are_well_ordered(self, adapter: ChatStreamingCapable): ...
    # ... one test class per Protocol in §2, applied to whichever Protocols a given adapter claims
```

A provider that fails any conformance test is registered (visible in admin — §5's manifest
system doesn't hide it) but **marked unavailable to the router** until it passes. This is what
turns "no provider-specific logic leaks" from a code-review convention into something CI
enforces: the fault-injection tests fail loudly if an adapter's error mapping is wrong, which
is exactly the failure mode that would otherwise let provider-specific behavior leak upstream
through an incorrectly-classified `ProviderErrorKind`.

---

## 10. Local models as a first-class citizen, not a fallback

`docs/MAINAI_ARCHITECTURE.md`'s framing — "MainAI as an eventual substrate for any AI,
including a fully local one" — is only real if local inference is architecturally equal to
cloud providers, not a bolted-on special case:

- **Idag:** `OllamaProvider` already implements the exact same `LLMProvider` interface as
  every cloud provider — no special-casing in `registry.py` or `chat.py`. This is the right
  instinct, already present, and this document's capability-based redesign (§2) preserves it
  exactly: Ollama becomes an adapter implementing `ChatCapable` + `EmbeddingCapable`, with a
  manifest declaring `data_residency.on_premises = true` and `tier = ModelTier.LOCAL`.
- **Målarkitektur:** additional local-inference adapters (vLLM, llama.cpp, text-generation-inference,
  a future in-house-hosted model) are, structurally, nothing new — each is one more manifest +
  adapter pair. The thing that changes over time isn't the architecture, it's the **routing
  policy default** (§6's `LocalPreferredStrategy`) — an organization (or MainAI itself, over
  time) can shift traffic from cloud to local/first-party inference by changing a policy
  value, not by touching a single line of `app/rag/`, `app/routers/`, or the Trust Engine.
  This is the precise mechanism that makes "eventually replace every external AI" an
  architectural property rather than a roadmap slide.

---

## 11. Migration path — additive, not a rewrite

Every step below can ship independently; each is a strict superset of what exists, and no
step requires touching `app/rag/`, `app/routers/chat.py`'s business logic, or the Trust Engine
beyond swapping which types they import.

| Phase | What changes | What doesn't |
|---|---|---|
| **0 (idag)** | `LLMProvider` ABC, `chat()`/`embed()`, flat `_PROVIDERS` dict, `chat_with_fallback()`, `UsageLog` for successes | — |
| **1** | Introduce §3's universal DTOs (`ChatRequest`/`ChatResponse`/`UniversalMessage`) and §7's `ProviderErrorKind` taxonomy. Existing provider classes get a thin adapter wrapper translating today's `Message`/`ChatResult` to/from the new types — **zero changes to the six existing provider implementations' actual HTTP-calling logic** | Router logic, fallback order, registry structure |
| **2** | Replace the flat `_PROVIDERS` dict with `ProviderManifest` + `ProviderRegistry` (§5). Existing providers each get a manifest (mechanical — capability set is already known: all six do `CHAT`+`EMBEDDING` except Ollama-style local runners, which stay the same two) | Adapter internals, application call sites |
| **3** | Introduce `CircuitBreaker` + `RetryPolicy` + `UsageRecorder`-driven `CostOptimizedStrategy`/`LatencyOptimizedStrategy` (§6) as additional `Router` strategies, defaulting to today's `PriorityOrderStrategy` behavior so nothing changes until a strategy is explicitly selected | Default runtime behavior (opt-in) |
| **4** | §9's conformance suite becomes a CI gate for any new adapter PR | Existing adapters (retrofitted, not blocked) |
| **5** | §8's per-tenant `ProviderPolicy` — requires `Organization` (`docs/MAINAI_ARCHITECTURE.md` §4 target) to exist first; not started until that multi-tenancy work has a real second-tenant driver, per that document's own stated avoidance of speculative build-ahead |
| **6** | New capabilities (`VISION`, `TOOL_USE`, `TRANSCRIPTION`, `TTS`) — additive Protocols (§2) and new manifests; existing `CHAT`/`EMBEDDING` adapters and call sites are completely unaffected | Everything built in phases 1–5 |

Nothing here is a placeholder — each phase is independently shippable, independently
testable, and each one's absence today is explicitly why the corresponding row in
`docs/MAINAI_ARCHITECTURE.md`'s closing "byggt kontra designat" table for §7 says
"designat, inte byggt."
