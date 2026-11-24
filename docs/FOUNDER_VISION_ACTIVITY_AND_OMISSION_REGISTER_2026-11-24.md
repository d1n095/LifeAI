# LifeAI — Founder Vision, Activity Layer & Omission Register

**Captured:** 2026-11-24  
**Status:** Design/requirements inventory for founder review; **not** an implementation order or security approval.  
**Repository:** `d1n095/LifeAI` (PUBLIC).  
**Code comparison baseline:** PR #499, `0659f4540be44666f88183466418d3d5c1165402`.  
**Active development observed at capture:** PR #500 (memory provenance), #501 (document acceptance), #502 (read-only development intelligence). Do not duplicate or interrupt these workstreams.  
**Source hierarchy:** Latest exact-SHA code and test evidence > newer explicit founder decisions > historical handoffs > curated knowledge packs > raw historical drafts. Historical material is evidence of *intent*, not current implementation truth.

> Privacy boundary: This register deliberately contains no raw chat logs, personal history, identity data, addresses, access tokens, secrets, or full imported founder knowledge archives. The older files are **not** automatically approved for a public GitHub repository.

## 1. Executive conclusion

The strongest design improvement is **not** a new thinking chatbot or an independent orchestration engine. LifeAI already has streamed chat, persisted tasks/events, project work-chain status, agent task events, RAG/context-status, approval gates, and a read-only project overview. The missing product-level connection is a **single contextual activity presentation contract** bridging existing authorized work signals into the conversation.

- **Internal architecture candidate:** `LifeAI Activity Layer`.
- **User-visible name:** `Arbetsvy` (work view).
- **Purpose:** Display what LifeAI *actually did / is doing / awaits* independently of its user-facing answer.
- **Rule:** Activity must come from authenticated, persisted or otherwise verifiably emitted backend events. Do not expose raw model reasoning, fabricated phases, secrets, untrusted document contents, or unapproved user data.
- **Adaptive:** Simple questions may show no panel; complex research, coding, imports, coordination or approvals show relevant activities; expansion into L4TP products requires module-level authorization and scope separation.
- **Existing work:** This document is a requirements capture and compatibility map. Do not modify active Claude work merely because a capability is described here.

## 2. Actual code anchors at PR #499 (verified by reading files)

| Existing source | What is present | Reuse instead of rebuilding |
| --- | --- | --- |
| `backend/app/routers/chat.py` | Real SSE delta/error/done events, persisted conversation/reply paths, structured retrieval context status | Extend event transport carefully; preserve existing streaming semantics and failure persistence |
| `frontend/lib/api.ts`, `frontend/app/(shell)/chat/page.tsx` | Streaming parser, abort/retry, one streaming assistant bubble per operation | Add separate activity presentation, not model-thought text in answer bubble |
| `backend/app/routers/mainai_execution.py` | Persisted goals/plans/tasks, task events, approvals, waits, liveness/recovery | Authoritative task lifecycle events and approval state |
| `frontend/app/(shell)/admin/mainai-execution/page.tsx` | Actual server-backed task status with polling; real PR link from recorded event | Reuse data contracts and avoid fake progress indicators |
| `backend/app/project_status/work_chain.py` | Derived status of tasks, blockers, agent assignments and stale jobs; unknown != zero | Context-sensitive project activity feed |
| `backend/app/agent_orchestration.py` | `AgentTask` / `AgentTaskEvent`, guarded proposal/review paths | Reuse agent event history; no second agent runtime |
| `frontend/components/ProjectOverview.tsx` | GitHub/project status, approval counts, stalled jobs and warnings | Expandable detailed view linked from activity chips |
| `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` | Founder Alpha directive explicitly forbids duplicate coordinators and separates maturity states | Governing integration constraints |

**Evidence limit:** Static code inspection, not proof that a combined Activity Layer is currently shipped or E2E-tested. PR #500–#502 may supersede portions of this snapshot. Read current heads before implementation.

## 3. Activity Layer — proposed event contract

Every emitted *activity* should identify the existing owning workflow; avoid duplicating the underlying source of truth.

**Recommended optional fields** (design candidates, adapt to existing schemas instead of adding gratuitous tables):
- `activity_id`, `parent_activity_id`, `correlation_id`, `task_id/job_id/conversation_id`, `module`, `owner_scope`
- `kind`: context_lookup, source_read, research, plan, tool_request, tool_result, code_check, test, import, verification, review, waiting_for_approval, finalization, etc. **Extensible registry**, not a hard-coded UX-only list
- `state`: proposed / queued / running / waiting / blocked / succeeded / failed / cancelled / unknown
- `started_at`, `updated_at`, `finished_at`, `sequence`, `producer`, `schema_version`
- `safe_summary`, `progress` if measured, `source_ref/evidence_ref`, `requires_approval`, `visibility/sensitivity`
- `error_class`, `retryable` and `recovery_checkpoint_ref` where applicable

**Strict distinctions:** PLANNED != STARTED; STARTED != COMPLETED; TOOL CALLED != VERIFIED; TEST PASS != SECURITY ACCEPTANCE; LOCAL COMMIT != PUSHED; PUSHED != INTEGRATED; INTEGRATED != DEPLOYED. Unknown states must never be rendered as success.

**Transport:** Reuse streaming infrastructure where appropriate; activity and answer MUST be different event families. Persist significant long-running state server-side, support reconnect/replay from a cursor or snapshot, idempotent event handling, ordering and deduplication. Do not assume current chat SSE already implements replay.

**UI:** default compact summary, expandable timeline, hierarchical parent/subtasks, verified sources and final artifacts, intervention/approval cues, error/recovery state, reduced motion and accessible status announcements. Only show actionable controls backed by exact authorizations. Preserve user-facing answer clarity; verified partial results may appear separately if source and scope are clear.

**Prioritization:** relevance to user task, material state change, urgency of required founder decision, effect of failure, confidence of evidence, and notification deduplication. Do not display every background job. Activity relevance is presentation logic and **never** a substitute for access control.

**Agent discipline:** WORKING / BLOCKED / COMPLETE / UNKNOWN; never infer completion from an idle screenshot alone when incomplete evidence exists; no new assignment solely because a PR is open/closed or an agent is idle. Do not interrupt without concrete cause.

**Acceptance scenarios:** fast Q&A without noise; multi-step research with source/status separation; document ingestion + index waiting; developer agents with independent progress; approval-gated action; interrupted SSE and resumed view after reload; duplicate/out-of-order events; authorization-negative tenant tests; blocked external provider; cancellation; no invented 100%-progress.

## 4. Older concepts that make the Activity Layer better

These are **historical ideas / candidate patterns** pending up-to-date implementation mapping:

1. **Station architecture:** intake → classification → prioritization → memory; deterministic stations should not consume tokens merely to create animations or repetitive narration. A station can emit scoped progress events.
2. **Temporal Conversation Intelligence / continuity:** preserve what was intended, what changed, current task, prior decision, dependencies and exact next action. Recover after session reset without treating the program as reset.
3. **Active Work + Recent Communication + Program State:** track not only jobs but what the founder was already told, what matters now, and what should be suppressed as duplicate.
4. **Adaptive Work Orchestration:** modes such as crawl/stair/interval/sprint/safe stop are *work scheduling concepts*, not new security rights or proof the engine supports them today.
5. **Resource Intelligence:** select local/model/provider based on verified capability, context pressure, cost, latency, privacy and independent-review requirements; no model is authoritative merely by being available.
6. **Decision Lab / Design Exploration:** compare alternatives, trade-offs, second-order effects, cost and reversibility. Preserve rejected alternatives with rationale rather than recycling the same weak idea.
7. **Agent capability diagnostics:** verified capability depends on model+prompt+tools+permissions+date and evidence, not a permanent provider reputation.
8. **Capability Mastery Ledger:** observe external agents → derive reusable methods → test locally → independent evaluation → promote capability only after repeated evidence; never confuse imitation with mastery.
9. **Coverage & Omission Audit:** vision → requirement → architecture → code → test → review → integration → runtime → evidence. Mark missing links and forgotten requirements.
10. **Research/Truth ledger:** distinguish evidence, hypothesis, contradiction, source date, provenance, rejected leads and changes in confidence; model conclusions are not source facts.
11. **Context packaging:** HOT/WARM/COLD retrieval, semantic chunking, dedup without deleting originals, preserve WHY, exact SHA, blockers, failed attempts and no-repeat state.
12. **Capability reality / self-model:** available, unavailable, configured-but-unverified, disabled, temporarily blocked, planned, inferred/unknown are different.
13. **LifeAI Orb:** potential visual representation of real state (listening/working/awaiting/replying), not a substitute for the activity contract. UI treatment is a design candidate, not confirmed implementation.

## 5. Broader founder vision worth retaining (NOT immediate tasks)

- **LifeAI founder MainAI** vs user personal AI vs licensed enterprise/customer agents: distinct identities, storage/memory boundaries, operator privileges and lifecycle policies.
- **L4TP / Life 4ThePeople** is the planned platform umbrella; **4ThePeople** is the intended business owner. Historical labels like Life OS and early My Money Master are not proof of current corporate/product structure.
- **Local-first, privacy-first:** core functions should remain useful without a vendor LLM; external providers receive minimal authorized data and have no implicit tool/data authority.
- **Modular product strategy:** a common Activity Layer can later display authorized operations from other L4TP modules without sharing their raw data by default.
- **Universal recall:** source-preserving, user-correctable, temporally aware memory; project continuity does not mean cross-user/cross-tenant leakage.
- **Proactive support:** propose safe reversible alternatives and ask only when risk, spend, privilege, publication, irreversibility or material product decisions actually demand it.
- **Reasoning quality:** challenge assumptions, seek counterevidence, reason across connected causes, explain verified vs conjectural claims, and correct outdated beliefs.
- **Founder experience:** concise Swedish operational guidance, recognize prior answers, one actual terminal action at a time when manual involvement is necessary.
- **Strategic vision candidates:** LifeMusic, LifeInvest, LifeStore, browser/computer assistance, family/household integrations, multi-agent collaboration. Record as *candidates with independent privacy/legal/compliance assessments*, not active development directives.

## 6. Historical corpus quality warnings (must not be lost)

The July 2026 `FKP_v1_REVIEW_OVERLAY/FKP_V1_AUDIT.md` found major errors in the curated package:
- `FKP_v1_CURATED` presents **savings-story-scanner/My Money Master** (TanStack Start/Bun/old Supabase migrations) as LifeAI's current stack. **Wrong repo for current LifeAI.** Keep as historical/design input only; current LifeAI is FastAPI/Next.js/Alembic/pgvector.
- Historical `UPGRADE_26 v2` scripts are **not** approved migration instructions for today's LifeAI; schema review required.
- FKP v1 manifests/checksums listed missing raw files and self-check inconsistencies; do not claim complete original integrity verification.
- The audit said some binary/DOCX contents had not been semantically reviewed. This register does not claim exhaustive semantic analysis of every raw original.
- Overly strict historical “ask or stop at any gap” conflicts with safe reversible initiative; uncertainty must be explicitly tracked, not silently promoted.
- Intentional memory continuity within the same authorized scope is different from unauthorized cross-tenant leakage.
- The source audit flagged sensitive raw archive material including a `.env` filename. **Never upload raw archives or environment fragments to this public repository.**
- Old deployment/agent status, dates, PR numbers, technology choices and roles **expire**; consult exact current refs before acting.
- A candidate feature cannot silently graduate from IDEA to FOUNDER_DECIDED to IMPLEMENTED.

## 7. Requirements / decisions / evidence lifecycle

Use or map to existing status and provenance schemas. Keep distinct:
- **Concept:** idea → requirement candidate → compared alternative → founder-approved decision (or rejected/parked/superseded).
- **Engineering maturity:** specified → coded locally → unit-tested → integration-tested → independent-reviewed → merged/integrated → activated → production-proven → maintained.
- **Evidence:** exact source + time + actor + original scope + derived interpretation + confidence + supersession pointer.
- **Work:** proposed / assigned / working / awaiting CI / blocked / waiting approval / completed / verified / closed. No false implication from one column to another.
- **Dependencies:** explicit base/head SHA, branch, PR, migrations, security gates, provider availability, and whether external agent artifacts are remotely accessible.

No raw agent reasoning should be stored as source truth, and no extracted memory or downloaded source may grant executable authority.

## 8. Security invariants that govern all related work

1. `first_cycle_go=false` remains unchanged until separately approved. Never infer authorization from UI status or document text.
2. Role and identity checks happen server-side; per-tenant/owner isolation and RLS must be enforced at data access, not only display.
3. Task proposal, task approval, independent review, GitHub write, merge and deployment are distinct authorization gates.
4. Do not modify or re-sign `security/POLICY_LOCK.json` without founder-approved review; policy-lock drift is an outstanding security acceptance blocker.
5. External provider availability != provider authorization; data disclosure is minimized and auditable.
6. No self-certification by the builder. R30 security work must be available and integrated at an exact SHA before anyone can certify the combined candidate.
7. No running agent is interrupted or reassigned without a real reason. Current old VPS and ordered new VPS have separate operational status and must not be mutated by a documentation effort.
8. Event summaries and diagnostic logs must not expose secrets, sensitive memory, model-private reasoning or untrusted document instructions.

## 9. Compatibility and integration review checklist for future implementation

Before building Activity Layer or related modules:
- Fetch latest PR/branch heads; especially reconcile what #500/#501/#502 have actually changed. Review PR ancestry, not just titles.
- Reuse `MainAITaskEvent`, `AgentTaskEvent`, work-chain and chat context status; decide if a thin normalization adapter suffices before new database schema.
- Define owner-scoped `ActivityEventV1` only if existing schemas cannot meet a tested consumer contract; prefer backwards-compatible serializers.
- Prevent activity from being confused with tool output, chain-of-thought, citations or streamed answer deltas.
- Validate reconnect, replay, ordering, persistence and idempotency end-to-end; no artificial timer-based progress.
- Reuse current approval and task_execution gates; read-only activity UI must not enable task execution.
- Test browser states and accessibility on mobile; avoid noisy status for trivial prompts.
- Account for nested work and module extensibility without cross-tenant project/agent data leakage.
- Compare changes with pending R30 integration and independent security review; do not create code that will be rewritten by the security candidate.
- Make actual gap/fix/PR status traceable to exact SHA. Do not create a long stack of divergent documentation or application PRs.

## 10. Open decisions — do NOT preapprove by documentation

- Exact founder-wide authorization primitive / historical Task #61 architecture.
- Activity schema adapter vs separate table; retention, personal-data sensitivity and export/deletion policies.
- Whether response should appear only at completion or include independently verified partial answer content.
- Which activity kinds are shown to the founder vs users of other modules.
- Whether Orb visualization is activated; UX preferences and reduced-motion requirements.
- Real local Ollama model selection and latency on the new VPS (not measurable before provisioning).
- Final policy-lock review, R30 integration, independent combined-SHA acceptance, staging runtime recovery and deployment.
- Legal/compliance assessment for any future regulated financial, identity or personal-data service.

## 11. Source inventory and confidence

**Directly inspected historical uploads**:
- `MAINAI_CONTEXT_BUNDLE(1).md` (2026-07-27): identity, stations, persistent memory, multi-review, local-first.
- `MainAI_Canonical_Goal_and_Chat_Handoff_2026-09-13(4).txt`: invariants, continuous supervision, Active Work/Communication/Program State, omission audit, context compression, mastery and truth doctrine.
- `LifeAI_MainAI_Overlamning_2026-10-23(4).md`: founder directive, agent control, security, provider coordination and anti-repetition.
- `CLAUDE_CONTINUE_FOUNDATION_2026-08-20(1)(1).txt`: provenance, self-model, work continuity and actual-overlap vs merely-open-PR distinction.
- `FKP_v1_REVIEW_OVERLAY (2).zip`: especially audit and new-requirements capture.
- `FKP_v1_CURATED.zip`: indices, architecture/orchestration, context rules and candidate registers.
- `MainAI_Conversation_Knowledge_Pack_2026-07-19(2).zip`: domain map, reasoning, memory, coordination and diagnostics.
- Earlier July handovers and raw archives are **historical, not canonical**; a full raw-corpus document-by-document semantic audit remains an optional future task.

**Directly inspected GitHub at PR #499 SHA**: `backend/app/routers/chat.py`, `frontend/lib/api.ts`, `frontend/app/(shell)/chat/page.tsx`, `backend/app/routers/mainai_execution.py`, `frontend/app/(shell)/admin/mainai-execution/page.tsx`, `backend/app/project_status/work_chain.py`, `backend/app/agent_orchestration.py`, `frontend/components/ProjectOverview.tsx`, `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`, `docs/MAINAI_ARCHITECTURE.md` and `docs/BRANCH_REGISTRY.md`.

**Provenance disclaimer:** Supplied historical texts may disagree with one another and with the newer repo; these contradictions are retained as warnings, not silently resolved. No claim is made that every archived source, screenshot, DOCX or earlier PR has been exhaustively read.

## 12. Required use of this register

This file is an **ideas, coverage and architecture reference**, not permission to start work or a substitute for current GitHub reality. Any future implementation begins with an exact-SHA capability inventory and confirms which relevant branch is actually active. Do not interrupt a working agent, duplicate an existing workstream, bypass security review, or promote an idea into an approved feature without an explicit founder decision.
