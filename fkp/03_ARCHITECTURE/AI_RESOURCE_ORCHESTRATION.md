# AI Resource Orchestration
**Sources:** `07_MULTI_AGENT_ORCHESTRATION_AND_MODES.md`, `06_AI_DIAGNOSTIC_AND_CAPABILITY_SYSTEM.md` (MainAI_Conversation_Knowledge_Pack_2026-07-19), in-conversation directive 2026-07-19.
**Carried forward unchanged from FKP v1.0** — stack-agnostic design, no LifeAI implementation exists yet (see `03_ARCHITECTURE/BUILT_VS_DESIGNED.md`).
**Status:** DESIGNED. No implementation yet. Binding architectural standard.
**v1.1 note on "Prohibited behaviors" below:** the founder's 2026-07-19 instruction ("don't stop to ask, work on independently, ask only when truly blocked by secrets/cost/irreversible action/a foundational product decision") narrows how strictly an agent should invoke a stop/ask path in ambiguous-but-safe situations. This document's specific prohibitions (claiming unverified access, exceeding budget without checkpoint, irreversible action without approval) are unaffected and remain binding — see `08_HANDOVER/AGENT_CONTEXT_RULES.md` §"H-01 amendment" for the precise scope of the narrowing.

---

## Purpose
MainAI must never stop working because one AI provider is unavailable. A provider's quota, session limit, or downtime is a routing condition, not a project blocker. Other safe, independent tasks must continue.

## Available AI providers and local models

| Agent | Intended strengths (routing hypotheses — must be verified by diagnostic) | Tools and access | Cost class | Known limits |
|-------|-------------------------------------------------------------------------|-----------------|------------|--------------|
| Claude Code | Repository reasoning, implementation, tests, CI, code review, refactoring | GitHub, file system, bash, test runners | Low-medium | Context window limit; no direct web access without tool |
| ChatGPT / Codex | Planning, synthesis, research, debugging, user guidance, review, structured output | Web search, code interpreter | Low-medium | API cost per call; cannot automatically control free web chat |
| Gemini | Multimodal (images, video), large-context inspection, document analysis | Web, image/video | Low-medium | Quota per day; schedule OCR already uses Lovable's Gemini gateway |
| Kimi | Long-form analysis, comparison, structured document work | Web | Low | Availability and quota unknown until verified |
| Lovable | Visual frontend prototyping, UI iteration, Supabase integration | Lovable platform | Credit-based | Credits per generation; not for architecture decisions |
| Local model | Private, repetitive, cheap background tasks: classification, summarization, search, private material | Local only | Very low / free | Must pass diagnostic threshold before real tasks; availability depends on hardware |
| MainAI | Memory, work coordination, knowledge promotion, agent handover, project graph | All above via orchestration | N/A | Cannot autonomously escalate its own authority |

All entries above are **routing hypotheses**. Capabilities must be verified by behavioral diagnostic before trust is extended. Source: `06_AI_DIAGNOSTIC_AND_CAPABILITY_SYSTEM.md`.

## Agent capability profile schema

```yaml
agent_profile:
  agent_id: string
  configuration_hash: sha256   # hash of model + system prompt + tools + permissions
  tested_at: ISO-8601
  provider: string
  model: string
  system_prompt_version: string
  tools: []
  memory_scope: []
  permission_scope: []
  capabilities:
    context_reasoning: { score: 0-100, confidence: low|medium|high, evidence: [] }
    code_generation:   { score: 0-100, confidence: low|medium|high, evidence: [] }
    # ... per capability
  restrictions: []
  approved_tasks: []
  prohibited_tasks: []
  required_approvals: []
  preferred_handoffs: {}
  operational_state:
    availability: available | limited | unavailable
    available_after: ISO-8601 | null      # when quota resets
    quota_remaining: number | unknown
    context_window_used: percent | unknown
    context_pressure: low | medium | high
    error_rate: number | null
    latency_ms: number | null
    cost_class: free | low | medium | high
    last_verified: ISO-8601
```

## Resource-aware routing rules

1. Before assigning a task, check `operational_state.availability` and `context_pressure`.
2. If the preferred agent is unavailable: record `available_after`, checkpoint active work, route independent tasks to the next best verified agent.
3. Do not duplicate completed work when the primary agent becomes available again — resume from handover.
4. Manual web chats (ChatGPT, Gemini web) can contribute artifacts at no additional API cost via human copy/paste and handover packages. MainAI must not pretend these are automated API calls.
5. Local models handle tasks only after passing the diagnostic threshold. Below threshold: route to cheapest available API provider.

## Context and cost budget per task

Every task assignment includes:
- `max_context_tokens`: hard limit for this task
- `max_cost_usd`: hard spend limit
- `max_duration_seconds`: timeout
- `context_priority_package`: which knowledge sections to load (not all — only what the task needs)

When an agent approaches 80% of any limit:
1. Save current checkpoint with handover
2. Mark task as `paused`
3. Record `available_after` for this agent
4. Route remaining independent subtasks to another verified agent
5. Queue agent-specific subtasks for resumption

## Checkpoint and handover contract

Every handover includes:
- Task ID and intended outcome
- Context package version used
- Work performed (with evidence)
- Changed artifacts (with checksums)
- Decisions made and their authority
- Assumptions (labeled as such)
- Risks identified
- Unresolved questions
- Exact next microstep
- Do-not-do list
- Resource state at time of handover

## Local-first routing policy

1. Attempt with local model if: task is routine, private, not safety-critical, and model has passed diagnostic.
2. If local model unavailable or below threshold: cheapest available API provider.
3. External API providers only for tasks requiring their specific capabilities (multimodal, large context, specialized reasoning).
4. No provider is a permanent dependency. Routing changes as diagnostics are updated.

## Prohibited behaviors

- An agent must not claim to have read a file, page, or source it did not actually inspect in this session.
- An agent must not present its routing hypothesis as proven capability.
- An agent must not exceed its cost or context budget without checkpointing.
- An agent must not perform irreversible actions (merge, deploy, delete, publish, spend) without explicit founder approval.
- A provider's quota exhaustion must never be presented as project failure — only as a routing condition.

## Operational modes

| Mode | Description | When used |
|------|-------------|-----------|
| Crawl | One microstep, then confirmation | High-risk tasks, first runs, untested agents |
| Stair | One verified step with checkpoint | Normal productive work |
| Interval | Work bursts with handover + resource recovery between | Long tasks, approaching context limits |
| Sprint | Multiple pre-approved low-risk steps before checkpoint | Well-understood, low-risk, fast tasks |
| Safe stop | Save complete state; stop immediately | Context/quota/risk/evidence inadequate |

Mode is selected from: task risk, founder preference, agent availability, cost, and context pressure.
