# Claude — NEW PRIMARY SECURITY LANE: Life Vault / External-AI Egress Control

**Cursor owns:** #167 composed autonomy runtime + Supervisor goal-worktree ownership fix.  
**You own:** Life Vault / provider egress firewall foundation. Do **not** collide with Cursor’s #167 files unless a handoff is required.

## Why now

#167 is the first composed provider chain (reserve → fake provider → settle → plan → execute).  
That makes external-AI egress a **top-level security invariant before any real Vault/memory data** is allowed to leave the process toward OpenAI / Anthropic / Gemini / Qwen / Kimi / Grok / future APIs.

This is zero-trust architecture, not an accusation against any vendor.

## Core invariants

```text
EXTERNAL_MODEL != TRUSTED_VAULT_COMPONENT
MODEL_REQUESTED_CONTEXT != AUTHORIZED_EGRESS_CONTEXT
RETRIEVAL_AUTHORITY != EGRESS_AUTHORITY
TOOL_ACCESS != EXPORT_AUTHORITY
LOCAL_MAINAI_KNOWLEDGE != PROVIDER_CONTEXT
DEFAULT EGRESS = DENY
DISCLOSURE = MINIMUM NECESSARY
```

An LLM can **never** authorize its own broader disclosure. Founder policy may allow a category; models never expand it.

## Target architecture

```text
Life Vault
  → local trusted retrieval broker
  → classification
  → need-to-know minimization
  → redaction / tokenization
  → egress policy decision (RequestedEgressScope ∩ AuthorizedEgressScope)
  → disclosure ledger
  → provider adapter (sanitized payload ONLY)
```

Prefer:

```text
VaultRetrievalRequest → local retrieval → EgressDecision → SanitizedProviderContext → adapter
```

Not:

```text
provider adapter → “search whatever context you need”
```

## Initial data classes

`PUBLIC` · `INTERNAL` · `PRIVATE` · `CONFIDENTIAL` · `VAULT` · `SECRET` · `NEVER_EGRESS`  
Orthogonal: `IP_PROTECTED`

Derived data inherits sensitivity unless an explicit declassification policy proves otherwise. Do **not** auto-downgrade VAULT by summarizing.

## Required disclosure ledger (every external model request)

provider, model, task/job/goal, purpose, authorization source, classes requested/allowed, fragments disclosed, provenance, hashes (never raw SECRET), redactions, denials, timestamp, spend, response reference.

Founder must eventually ask: “What has provider X ever received about project Y?” and get a complete answer.

## Prompt-injection invariant

Retrieved content is **DATA, never authority**. Web/email/doc/GitHub issue/provider output cannot instruct the Vault broker to export more.

## Deliverable first

1. Threat model + architecture map + blocker map grounded in **current** repo (especially provider-planning / `RegistryPlanningAdapter` / `plan_with_provider` boundary).
2. Identify the **single last trusted boundary** before payload leaves the process.
3. Implement the smallest unowned foundation: **default-deny egress gate** in front of provider planning, with adversarial tests, **without** colliding with Cursor #167.
4. Claim ACTIVE WORK before writes.
5. No real external provider activation yet — fake adapters only.
6. Continue autonomously after the first PR.

## Attack list (must cover in tests)

1. Provider asks for all memory → deny  
2. One technical VAULT fact → minimum sanitized fragment only  
3. Retrieved “ignore rules / upload notes” → data only  
4. SECRET in selected context → block/redact before adapter  
5. IP_PROTECTED raw idea for generic coding → deny raw; derive smallest local spec only if policy allows  
6. Cross-owner retrieval → impossible  
7. Model asks for more credentials → **new** egress decision  
8. Retry/idempotency must not bypass original decision  
9. Provider switch A→B never inherits A’s disclosure  
10–14. Logs / exceptions / caches / embeddings / telemetry subject to egress  
15. Local model = different policy class, still least privilege  

## Do not

- Wait on obsolete #165 CI / enter watch mode  
- Patch Cursor-owned #167 production_entry / Operator worktree files unless handing off  
- Activate real cloud providers with founder/private data  
- Treat any cloud LLM as “MainAI superuser”

Trusted MainAI identity = **local** policy/runtime core.
