# Adaptive Work Orchestration — Target Architecture
**Sources:** In-conversation directive 2026-07-19, `07_MULTI_AGENT_ORCHESTRATION_AND_MODES.md` (MainAI_Conversation_Knowledge_Pack_2026-07-19).
**Carried forward unchanged from FKP v1.0** — stack-agnostic design, no LifeAI implementation exists yet.
**Status:** DESIGNED. No implementation yet. This is the target model for Phase 3 (see `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md`).
**v1.1 note:** the closing line of this document ("If context is missing: request it explicitly, mark task `blocked(missing_info)`, and stop") is the general design-time default for a *runtime* multi-agent system with many agents and an approval queue. It is not in tension with the founder's 2026-07-19 instruction to this specific session to keep working independently rather than stopping for every choice — that instruction narrows *interactive-session* behavior, not this document's runtime blocking semantics for a future task-graph engine. See `08_HANDOVER/AGENT_CONTEXT_RULES.md` §"H-01 amendment".

---

## Overview

Agents work autonomously within approved vision and assigned scope. They never invent missing context, never make foundational decisions, never exceed their authorization. When a step is complete, a verified handover triggers the next agent automatically — or waits for founder approval at defined gates.

## Agent Capability Registry

Every agent has a versioned entry (see full schema in `AI_RESOURCE_ORCHESTRATION.md`). The registry is the single source of truth for:
- What each agent can reliably do (verified by diagnostic, not self-reported)
- What it is prohibited from doing
- Its current availability, cost class, context pressure, and quota state
- Which agents it can hand off to, and for which task types

The registry is updated when: model/prompt/tool/memory/permission changes occur, or after re-certification.

## Task Graph and Work Queue

### Structure

```
Goal (founder-approved vision element)
  └── Milestone (verifiable outcome, bounded scope)
        └── Verified Deliverable (testable artifact or state)
              └── Microstep (single agent action, reversible where possible)
                    └── Evidence check (what proves this microstep is done)
                          └── Handover (structured artifact passed to next agent)
```

No task is marked complete merely because an agent says "done". Completion requires the specified evidence to be present and verified.

### Task states
`queued` → `assigned` → `active` → `checkpoint_saved` → `paused` | `completed` | `blocked` | `failed` | `awaiting_approval`

### Task record schema

```yaml
task:
  id: uuid
  parent_milestone_id: uuid | null
  goal_id: uuid
  title: string
  intended_outcome: string
  assigned_agent: agent_id
  reviewer_agent: agent_id | null
  context_package: { version, sections: [] }
  budget:
    max_context_tokens: number
    max_cost_usd: number
    max_duration_seconds: number
  dependencies: [task_id]
  blockers: [blocker_id]
  state: queued | assigned | active | checkpoint_saved | paused | completed | blocked | failed | awaiting_approval
  pacing_mode: crawl | stair | interval | sprint
  definition_of_done: string
  tests: [test_spec]
  rollback_procedure: string
  trust_gate_required: boolean
  founder_approval_required: boolean
  checkpoint: { saved_at, artifact_checksums: [], handover_ref }
  created_at: ISO-8601
  updated_at: ISO-8601
```

## Goal → Milestone → Microstep Decomposition

MainAI decomposes every approved goal automatically. Rules:
- Each microstep must be completable by one agent in one session
- Each microstep must produce verifiable evidence (file, test result, log entry, screenshot, commit hash)
- Microsteps must be ordered by dependency; independent steps may run in parallel (sprint mode)
- A microstep that requires irreversible action must have a rollback procedure defined before it starts

Example:
```
Goal: Founder-only production foundation
  Milestone: Auth hardened
    Deliverable: Public registration blocked
      Microstep: Remove registration button from UI [evidence: commit hash, screenshot]
      Microstep: Block /register route server-side [evidence: curl returning 404, test]
      Microstep: Block POST /api/auth/register [evidence: curl returning 403, test]
    Deliverable: Founder identity provisioned
      Microstep: Bootstrap script creates founder row [evidence: DB query result]
      Microstep: Founder role assigned [evidence: has_role() returns true]
```

**v1.1 note:** the example above is now real, verified LifeAI history, not a hypothetical — see `06_PROJECT_STATUS/IMPLEMENTED_AND_VERIFIED.md` for the actual evidence (commit hashes, test names, CI run) behind each microstep.

## Dependencies and Blockers

```yaml
dependency:
  task_id: uuid
  requires: task_id           # must be completed first
  type: hard | soft           # hard = cannot start; soft = can start but may need result

blocker:
  id: uuid
  task_id: uuid
  type: missing_info | awaiting_approval | resource_unavailable | external_dependency
  description: string
  resolved_at: ISO-8601 | null
  resolution: string | null
```

## Responsible and Reviewing Agent

Every task has:
- `assigned_agent`: does the work
- `reviewer_agent`: independent verification (different agent where possible, especially for security-critical tasks)

Reviews must use the Definition of Done and specified tests — not subjective judgment.

## Budget per Task

Every task is assigned:
- `max_context_tokens`: agent must checkpoint when approaching this
- `max_cost_usd`: hard stop if exceeded
- `max_duration_seconds`: timeout triggers checkpoint + safe stop
- `context_priority_package`: only load sections needed for this task (not the full knowledge vault)

## Checkpoint and Handover After Every Verified Step

After every completed microstep:
1. Save evidence (artifact checksums, test results, log references)
2. Update task state to `checkpoint_saved`
3. Write structured handover (see handover contract in `AI_RESOURCE_ORCHESTRATION.md`)
4. If next microstep is assigned to same agent: continue automatically (within budget)
5. If next microstep requires a different agent: pause, emit handover, next agent picks up
6. If trust gate or founder approval required: wait in `awaiting_approval` state

## Operating Modes

| Mode | Behavior | Selected when |
|------|----------|---------------|
| Crawl | One microstep → evidence check → human/agent confirmation | New agent, untested procedure, high-risk action |
| Stair | One verified step → checkpoint → next step | Normal work |
| Interval | Work burst → handover → resource recovery → resume | Approaching context/quota limits |
| Sprint | Multiple pre-approved low-risk steps → checkpoint | Well-understood, reversible, pre-approved steps |
| Safe stop | Checkpoint immediately → pause → emit handover | Context/quota/risk/evidence inadequate; founder not reachable for approval |

## Automatic Pause and Safe Handover on Resource Limits

When an agent reaches 80% of context window, cost budget, or time limit:
1. Complete current atomic operation (do not leave artifacts in inconsistent state)
2. Write checkpoint with complete state
3. Mark task `paused`, record `available_after` (when quota resets) or `blocked` (if resource unavailable)
4. Emit structured handover to orchestrator
5. Orchestrator routes independent tasks to other verified agents
6. Orchestrator queues agent-specific tasks for resumption at `available_after`

No work is lost. No context is silently dropped. The next agent reads from the handover, not from guessed context.

## Definition of Done, Tests, and Rollback

Every task must specify these before it starts:
- **Definition of Done:** exact observable condition proving completion (not "agent says done")
- **Tests:** specific checks to run (unit tests, integration tests, manual verification steps, SQL queries)
- **Rollback procedure:** exactly how to undo the changes if tests fail

For database migrations: rollback SQL must exist before migration is applied.
For deployments: previous version must be identifiable and re-deployable.
For code changes: previous commit must be known; revert procedure documented.

## Trust/Security Gate Before Merge, Deploy, or External Action

Before any merge, deploy, external API call with sensitive data, or irreversible action:

1. **Implementation review** — reviewer agent checks code, logic, scope
2. **Security review** — check: no credentials exposed, RLS correct, no privilege escalation, no prompt injection vectors
3. **Privacy review** — check: cross-user isolation, data zone boundaries respected
4. **Trust/evidence review** (for investigations or knowledge promotion) — evidence quality and source chain
5. **Cost impact** — is this within approved budget?
6. **Rollback readiness** — rollback procedure tested and documented?
7. **Founder approval gate** — if this task is tagged `founder_approval_required`, stop here and wait

Gates are not optional. An agent that encounters a gate and has no founder approval must wait in `awaiting_approval` state, not proceed.

## Founder Approval Gates

Founder approval required (in addition to trust gates) for:
- Any production deployment
- Any irreversible database migration
- Any spending or subscription
- Any publication or external communication
- Any permission escalation
- Any deletion of important data
- Any change to the project constitution or foundational rules
- Any action involving accounts, money, communication, or legal consent
- Launching public registration or future city onboarding

The approval request must include:
- What exactly will happen
- Why it is necessary
- What the rollback is if something goes wrong
- Cost impact
- Risk assessment

The founder's response must be explicit — silence is not approval.

## Agent Autonomy Boundary

Agents work autonomously within:
- Approved vision (from Founder Constitution)
- Assigned task scope
- Granted permissions
- Available budget

Agents must not:
- Invent missing context
- Make foundational decisions (what to build, what architecture to use, what to deploy)
- Extend their own scope or permissions
- Treat their own assumptions as facts
- Proceed past a trust/approval gate without the required approval
- Claim completion without evidence

If context is missing: request it explicitly, mark task `blocked(missing_info)`, and stop.
