# MainAI V2 — Local Specialist Workforce (Stage V2-E)

**Status:** design-only, isolated lane (branch `claude/mainai-v2-workforce-identity`). Does
not touch PR #245 / candidate SHA `818dfb732da47901eb5ae06ffdd9c829fe00c4c5`. References
`docs/mainai_v2/MAINAI_V2_ARCHITECTURE_MAP.md` (V2-A) for vocabulary and the constitution.

## 0. What already exists — extend, do not rebuild

This is the single most important section of this document. The founder's brief describes a
Local Specialist Workforce; this codebase already has a real, tested **MainAI Internal
Workforce Foundation** (Stage T, `app/workforce/`, PRs #230-234, #243, #245). Read before
building anything new:

- `WorkforceAgentProfile` (`app/models/workforce.py`) — owner-scoped agent identity:
  `agent_key`, `name`, `role`, `agent_type`, `trust_zone`, `capability_tags: list`,
  `allowed_tool_classes: list`, `default_context_class`, `risk_tier`, `cost_class`, `status`
  (starts at `candidate`), `provenance`.
- `WorkforceAssignment` — the actual bounded, per-task authority envelope:
  `allowed_read_paths`, `allowed_write_paths`, `allowed_tool_classes`,
  `allowed_network_destinations`, `allowed_project_ids`, `spend_ceiling_usd`,
  `allow_execution_effects`, `expires_at`, `verification_status`, `verifier_profile_id`.
- `CapabilityRecord` (`app/models/capability_reality.py`) — `capability_key`, `domain`,
  `status`, `confidence`, `last_verification_evidence_id`, `last_verified_at`,
  `last_success_at`, `last_failure_at`.
- `app.evidence_claim.evidence_supports_claim()` — the shared gate (fixed tonight, PR #245)
  requiring exact subject match + a real positive outcome before anything can be marked
  `verified_available`.
- `app.workforce.department_evidence.department_capability_ledger()` — per-department
  inspectable status, "does NOT promote candidates automatically."

**This document's job is NOT to design a new agent system.** It is to design how the
founder's domain content — specific finance/legal/security specialists, their knowledge-pack
bindings, and a richer competence lifecycle than today's `candidate`/`active`/`retired`
status — extends these exact primitives.

## 1. Agent Contract — mapped to existing fields

| Founder's field | Existing mapping | New? |
|---|---|---|
| `role` | `WorkforceAgentProfile.role` | Reuse as-is |
| `domain` | `WorkforceAgentProfile.provenance["domain"]` today (no dedicated column) | **New column** `domain: str` — promote out of provenance since department_evidence already groups by an implicit domain concept; deserves a real, indexed field |
| `skills` | `WorkforceAgentProfile.capability_tags` | Reuse as-is |
| `allowed_tools` | `WorkforceAgentProfile.allowed_tool_classes` (profile-level ceiling) + `WorkforceAssignment.allowed_tool_classes` (per-task, must be a subset) | Reuse as-is — the two-tier shape (profile ceiling, assignment subset) already matches `SPECIALIZATION != BROADER ACCESS` |
| `allowed_data_classes` | `WorkforceAgentProfile.default_context_class` + `WorkforceContextPackage.denied_kinds` at assignment time | Reuse as-is |
| `network_policy` | `WorkforceAssignment.allowed_network_destinations` | Reuse as-is |
| `vault_policy` | Not modeled today — `WorkforceContextPackage.denied_kinds` covers vault-kind denial for external trust zones, but there's no explicit per-agent vault policy field | **New column** on `WorkforceAgentProfile`: `vault_policy: str` (`"never"` / `"read_derived_only"` / `"explicit_grant_required"`), default `"never"` |
| `risk_ceiling` | `WorkforceAgentProfile.risk_tier` | Reuse as-is |
| `execution_scope` | `WorkforceAssignment.allow_execution_effects` + the whole envelope | Reuse as-is |
| `verification_requirements` | `WorkforceDelegationRequest.verification_requirement` + `policy_for_risk()` in `app/workforce/verification.py` | Reuse as-is |
| `lease_duration` | `WorkforceAssignment.expires_at` | Reuse as-is |
| `local_model_requirement` | `WorkforceAgentProfile.provider_type`/`provider_model_id` (today describes what IS configured, not a hard requirement flag) | **New column** `require_local_model: bool`, default `False` — when `True`, `resolve_delegation()` must refuse to select this agent if the resolved model isn't local (ties into Sentinel/Privacy: a local-only specialist must never silently escalate to a cloud model) |
| `competence_state` | Does not exist — today only `WorkforceAgentProfile.status` (`candidate`/`active`/`retired`) plus `CapabilityRecord.status` (per-capability, not per-agent) | **New**, see §2 |
| `knowledge_pack_versions` | Does not exist | **New column** `knowledge_pack_bindings: JSON` — `list[{pack_id, min_version, pinned_version | null}]` |

**Migration shape:** one additive migration on `workforce_agent_profiles` (4 new nullable/
defaulted columns) — no breaking change to existing rows or callers, matching this session's
own established migration discipline (additive-only, CHECK constraints, RLS already covers
the table).

## 2. Competence state machine

```
UNTRAINED -> LEARNING -> ASSISTED -> PROVEN_LOCAL -> EXPERT_LOCAL
                                          │              │
                                          └──────┬───────┘
                                                 ▼
                                              STALE
```

- **UNTRAINED**: no knowledge pack bound, or bound but zero exam attempts. Cannot be selected
  for any real delegation (`resolve_delegation()` must exclude it).
- **LEARNING**: at least one exam attempt exists, none passed with real evidence yet.
- **ASSISTED**: has passing exam evidence but `require_independent_verifier` stays forced
  `True` regardless of the assignment's own risk policy — an ASSISTED agent's `VERIFIED`
  decision can never itself close the loop without independent review (same shape as
  `app.workforce.verification`'s `require_independent_verifier`).
- **PROVEN_LOCAL**: `CapabilityRecord.status == "verified_available"` for every capability
  tag this agent claims, via the real gate (`evidence_supports_claim()`, not a shortcut).
- **EXPERT_LOCAL**: PROVEN_LOCAL **and** a minimum real-usage count (`>= N` genuinely
  independent-verified successes, mirroring #240's tonight-fixed "one lucky success != proven"
  bar of >=3 trials / >=0.66 rate applied to the DEPARTMENT LEDGER — this state machine reuses
  that exact bar, does not invent a looser one).
- **STALE**: the transition that structurally defends `PAST COMPETENCE != CURRENT COMPETENCE`.

**STALE transition triggers (both, not either/or):**
1. **Time-based:** `now - CapabilityRecord.last_verified_at > knowledge_pack.valid_until` OR
   a hard ceiling (e.g. 90 days) with no fresh verification, whichever is sooner — ties
   directly to the knowledge pack's own `valid_until`/`last_checked` fields (§4), so a
   specialist bound to a pack that itself went stale becomes STALE too, automatically, not by
   a separate timer someone has to remember to configure per-agent.
2. **Contradicting-evidence-based:** any new `CapabilityRecord.last_failure_at` that is more
   recent than `last_success_at` for the SAME capability_key immediately forces
   PROVEN_LOCAL/EXPERT_LOCAL -> STALE, synchronously, in the same transaction as the failure
   observation — not on a delayed batch job. This is the exact "older success + newer failure
   must not keep verified" invariant this session proved was broken (and partially fixed) in
   `record_capability_observation()` tonight, applied one level up at the agent-competence
   layer, not just the raw capability-record layer.

**STALE agents are treated identically to LEARNING for selection purposes** (can still be
assigned, but `require_independent_verifier` is forced `True` and the department ledger must
surface `"stale_since"` in its status, never silently re-report PROVEN_LOCAL). Recovering from
STALE requires a fresh, real passing exam — never a timer expiring back to trust.

## 3. Three worked examples

**Finance — `dept-finance-debt` (debt specialist):**
```
role="debt_resolution_specialist", domain="finance",
skills=["debt_negotiation_rules_se", "consumer_credit_act_se", "collection_letter_analysis"],
allowed_tools=["read_excerpt", "draft_document", "local_calculator"],
allowed_data_classes=["financial_documents", "correspondence"],
vault_policy="read_derived_only",
risk_ceiling="medium",   # drafting a response to a collector is medium, not low
execution_scope=allow_execution_effects=False,   # never sends anything itself
verification_requirements="independent_verifier",  # +founder_approval if risk escalates to "high"
lease_duration=24h,
require_local_model=True,   # debt details are sensitive -- never leaves the device
competence_state=PROVEN_LOCAL,
knowledge_pack_bindings=[{"pack_id": "se-consumer-credit-act", "min_version": "2026.1", "pinned_version": null}]
```

**Legal — `dept-legal-rental` (rental law specialist):**
```
role="rental_law_specialist", domain="legal",
skills=["rental_law_se", "eviction_procedure_se", "deposit_dispute_se"],
allowed_tools=["read_excerpt", "draft_document", "case_law_search_local"],
allowed_data_classes=["lease_documents", "correspondence", "photos_evidence"],
vault_policy="explicit_grant_required",
risk_ceiling="high",   # eviction-adjacent advice is genuinely high-stakes
execution_scope=allow_execution_effects=False,
verification_requirements="independent_verifier+founder_approval",  # risk=high policy, see app.workforce.verification
lease_duration=48h,
require_local_model=True,
competence_state=ASSISTED,   # newly onboarded, not yet PROVEN
knowledge_pack_bindings=[{"pack_id": "se-rental-law", "min_version": "2026.1", "pinned_version": null}]
```

**Security — `dept-security-malware` (malware analysis specialist, a Sentinel-domain agent):**
```
role="malware_triage_specialist", domain="security",
skills=["static_analysis", "behavior_sandbox_interpretation", "hash_reputation_lookup"],
allowed_tools=["read_excerpt", "sandbox_execute", "hash_lookup"],
allowed_data_classes=["process_metadata", "file_hashes", "sandbox_logs"],   # explicitly NOT finance/chat history -- SPECIALIZATION != SURVEILLANCE, see V2-D
vault_policy="never",
risk_ceiling="medium",
execution_scope=allow_execution_effects=True,   # quarantine actions are real effects, tightly scoped
verification_requirements="independent_verifier",
lease_duration=1h,   # short -- incident-scoped
require_local_model=True,
competence_state=EXPERT_LOCAL,
knowledge_pack_bindings=[{"pack_id": "security-threat-knowledge", "min_version": "2026.3", "pinned_version": "2026.3.1"}]  # pinned: threat data staleness is dangerous in the OTHER direction too, pin to a known-good version rather than always-latest for reproducible incident response
```

## 4. Interface to Offline Knowledge Packs (V2-F, written separately — interface only)

An agent declares dependency via `knowledge_pack_bindings: list[{pack_id, min_version,
pinned_version}]`. At selection time (`resolve_delegation()`), before an agent can be chosen:

1. Resolve each binding's actual currently-available pack version.
2. If `pinned_version` is set, require an exact match — refuse selection otherwise (fail
   closed, not "close enough").
3. If only `min_version` is set, require the available version to be `>= min_version` **and**
   the pack's own `valid_until`/`last_checked` (owned by the V2-F document, not redefined
   here) to not have expired.
4. If a bound pack is missing entirely or fails validation, the agent is NOT silently
   downgraded to "best effort" — selection fails closed for tasks requiring that skill, and
   the department ledger reports `"blocked_missing_knowledge_pack"` as the exact cause key
   (matching this session's own "detected blocker == enforced blocker, exact cause preserved"
   discipline from tonight's readiness fix).

This binding check is a NEW, small function (`assert_knowledge_packs_satisfied(agent, packs)
-> None`, raises on failure) called from `resolve_delegation()` alongside the existing
`assert_agent_selectable()` check — additive, does not modify existing selection logic for
agents with no pack bindings (e.g. the security malware specialist above, which uses packs but
plenty of existing Stage T agents have none and are unaffected).
