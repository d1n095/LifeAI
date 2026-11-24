# Competitor engineering research — techniques, not feature lists

**Status:** research + comparison, 2026-11-24. No competitor code was copied, vendored or
executed. Findings come from public documentation and engineering write-ups; they are
hypotheses about other systems, not runtime measurements of LifeAI. Every "LifeAI already
has" claim below is tied to a module in this checkout.

The program asked for valuable **engineering techniques**, compared against actual LifeAI
code, with implementation only where the improvement is missing, measurable and inside the
existing authorized scope. The short result: the safe techniques are already present in some
form, and the missing ones would either duplicate LifeAI's memory/RAG/orchestration layers or
touch areas owned by Claude (cognitive/UX) or Z.ai (security). They are recorded as proposals,
not silently implemented.

## 1. OpenHands — event log as the source of truth

**Technique.** An immutable, typed event log drives execution; the agent step is stateless
and rebuilds its view from the log. Conversation state is the only stateful component, so
pause/resume/confirmation are reads and writes of events rather than an in-memory message
list. Execution happens in a sandbox behind an action-execution API.

**LifeAI already has.** `agent_work_assignment_events` (append-only, trigger-enforced) and
`agent_dispatch_executions` (live attempt state, distinct from history);
`intelligence_governance` records execution evidence by id. `mainai_jobs` already separates
lease generation from job state.

**Gap.** LifeAI's atlas reads the checkout, not the event log. It therefore cannot say
`observed` from events, and correctly reports `observed=None`.

**Decision — proposal, not implemented here.** Deriving `observed` maturity from existing
`AgentWorkAssignmentEvent`/`IntelligenceEvidence` rows is a Phase 6/runtime-evidence job. It
needs a real worker path and an owner decision about which events count as observation;
fabricating it from a static checkout would violate the atlas's own honesty rule.

Source: <https://docs.openhands.dev/sdk/arch/events>,
<https://docs.openhands.dev/openhands/usage/architecture/runtime>.

## 2. Letta — labelled, size-limited memory blocks and sleep-time reflection

**Technique.** The context window is split into named blocks with a label, description,
value and character/token limit, always present in the prompt. A separate sleep-time agent
reflects on history and rewrites the primary agent's blocks, moving memory maintenance out of
the user's turn.

**LifeAI already has.** A persistent memory subsystem with provenance
(`memory_source_units`, `document_source_units`), project memory, and inspectable memory.
Prompt assembly is owned by the chat/RAG path.

**Gap / risk.** Copying memory blocks would create a **second memory representation** and
would overlap Claude's cognitive/prompt work — both explicitly out of scope. Sleep-time
reflection would add a background agent loop, which is a new orchestration system.

**Decision — reject for now.** The useful idea (explicit budgets and labelled context
regions) can be proposed to the prompt owner later, but it is not an atlas change and must
not be implemented unilaterally.

Source: <https://docs.letta.com/guides/core-concepts/memory/memory-blocks>,
<https://www.letta.com/blog/sleep-time-compute>.

## 3. Onyx — permission-syncing connectors and separated index/query capacity

**Technique.** Connector documents carry source permissions that are synced on a schedule;
access types distinguish public, private, source-synced and source-synced-plus-group.
Indexing and query embedding run on separate model servers so heavy ingestion does not
starve live search. Connector construction has one credential path.

**LifeAI already has.** Per-owner RLS, `documents.uploaded_by`, project scoping, storage
provenance, and separate worker/API processes. Security policy is owned by Z.ai.

**Gap / risk.** LifeAI has no external-connector permission sync because it has no
external-connector product. Adding one would touch the security boundary and is outside this
program.

**Decision — proposal only, for the security owner.** If connectors are added later, the
permission-sync matrix (public / private / source-synced / restricted) is a better starting
vocabulary than ad-hoc flags. Do not modify `security/` or Z.ai code here.

Source: <https://docs.onyx.app/security/architecture/access_controls>,
<https://docs.onyx.app/admins/connectors/overview>,
<https://docs.onyx.app/security/architecture/system_description>.

## 4. Agno AgentOS — durable sessions, run history and reliability evals

**Technique.** Sessions persist across restarts and replicas; every run is recorded; evals
assert expected tool calls and arguments (reliability), answer accuracy, and performance;
background execution is durable and cancellable. State lives in the database, not in a
replica.

**LifeAI already has.** Durable jobs with leases, `intelligence_governance` executions and
evidence, assignment history, and real Postgres tests. The atlas adds a capability map, not
a second run store.

**Gap.** There is no single eval suite that asserts "the agent called the expected tool with
the expected arguments" across assignments. Existing tests assert behaviour, but not a
reusable reliability corpus.

**Decision — proposal.** A reliability-eval corpus built on existing
`IntelligenceEvidence.payload` would be measurable and useful. It is not implemented here
because it needs a real dispatch path and would expand the test surface beyond the atlas's
scope; the atlas should first be reviewed.

Source: <https://docs.agno.com/agent-os/introduction>,
<https://docs.agno.com/evals/reliability/overview>,
<https://docs.agno.com/features/storage>.

## 5. OpenClaw — gateway control plane and explicit skills

**Technique.** A resident gateway owns sessions, channels and tool policy; plugins are small
modules; skills are `SKILL.md` packages with declared tools; sandbox backends are opt-in.
Memory is workspace markdown plus hybrid search.

**LifeAI already has.** A FastAPI control plane, agent coordination with path leases,
`security/agent_capabilities.json` (from PR #502), and a skill-like capability registry in
the coordination agent table.

**Gap / risk.** A second skill registry would duplicate the coordination agent capabilities
and PR #502's file. A resident gateway is an architectural change, not an atlas improvement.

**Decision — reuse the existing capability registry; no new registry.** The atlas's
`CAPABILITY_ALIASES` is a read-only lookup for recommendations, not a runtime skill system.

Source: <https://docs.openclaw.ai/agent-runtime-architecture>,
<https://docs.openclaw.ai/start/why-openclaw/openclaw-and-hermes-agent>.

## 6. Hermes Agent — session infrastructure with full-text recall

**Technique.** Sessions live in SQLite with FTS5 and WAL, track source tags and parent/child
lineage for compression splits, and are addressable before inference. A model-facing
`session_search` tool lets the agent decide when to recall rather than always injecting
history.

**LifeAI already has.** Message/conversation persistence and retrieval; the atlas does not
add another conversation store.

**Gap.** Whether LifeAI's recall is worse than FTS5 on the same corpus is unmeasured. Adding
a second full-text index would duplicate retrieval.

**Decision — proposal: measure recall first.** A bounded comparison on a fixed conversation
set would justify or reject a change; no code is written on the assumption that another
system's recall is better.

Source: <https://github.com/NousResearch/hermes-agent>,
<https://arize.com/blog/how-hermes-implements-open-source-agent-harness-architecture>.

## 7. NemoClaw — versioned blueprint, sandbox boundary, credential placeholder

**Technique.** A host CLI applies a versioned YAML blueprint (image, policies, inference
profile) through OpenShell. The agent sees placeholder credentials; an L7 proxy injects the
real value at egress. Egress is policy-controlled and lifecycle state is recorded outside the
agent process.

**LifeAI already has.** Environment-based provider configuration, activation gates,
`first_cycle_go`, and policy-lock verification. There is no general sandbox runtime.

**Gap / risk.** Introducing a sandbox/blueprint system is a production-infrastructure change
and would touch VPS/deployment concerns explicitly excluded by the program.

**Decision — out of scope; proposal only.** The transferable idea is "agent never sees the
real credential, only a placeholder resolved at the boundary". LifeAI's existing activation
and policy locks already aim at that property; changing them requires the security owner.

Source: <https://docs.nvidia.com/nemoclaw/reference/architecture>,
<https://docs.nvidia.com/nemoclaw/about/how-it-works>.

## 8. OpenDots / OpenBot — per-coworker permissions and isolated computers

**Technique.** Each "Dot" has its own role, instructions, tools and conversation; browser,
files, shell and memory are granted per Dot; each Dot can get an isolated computer whose
profile persists across restarts. AG-UI carries streamed messages/tool calls/state; durable
threads live in a separate service.

**LifeAI already has.** Per-agent coordination rows with role, read/write mode and path
scopes; assignment events; RLS at the data layer. The atlas's ownership lookup now answers
"does another assignment already cover this path?" using those exact rows.

**Gap / risk.** Isolated per-agent computers are infrastructure, not application code. A
second thread store would duplicate conversations.

**Decision — ownership lookup implemented; computer isolation out of scope.** The useful,
in-scope technique was the deterministic scope check, and that is what
`app/context_atlas/ownership.py` adds — reusing `paths_conflict_any`, not inventing a new
permission model.

Source: <https://github.com/CopilotKit/OpenDots>,
<https://github.com/CopilotKit/openbot/blob/main/docs/architecture.md>.

## 9. AnythingLLM / Open WebUI — workspace isolation and retrieval controls

**Technique.** AnythingLLM organises retrieval around workspaces so unrelated material is not
mixed; Open WebUI exposes hybrid BM25+vector search, reranking, top-k and relevance
thresholds, plus citations. Both keep document collections separate from chat threads.

**LifeAI already has.** Project/owner scoping, RLS, retrieval with provenance, and
`app/rag/trust.py` for contradiction handling. The atlas's candidate lookup is owner-scoped
for the same reason.

**Gap.** Retrieval tuning parameters are not centrally documented as a reviewable set; that
is a measurement task, not a code gap the atlas should guess at.

**Decision — reuse existing isolation; no new workspace model.** Adding a workspace layer
would duplicate `project_id` and RLS.

Source: <https://docs.openwebui.com/alternatives/anythingllm>,
<https://anythingllm.com/alternatives/open-webui>.

## 10. What was actually implemented from this research

Nothing was implemented *because* a competitor does it. The two gaps this phase did close
come from the program's own Phase 2 requirements and reuse LifeAI primitives:

1. **Path-scoped work ownership** (`app/context_atlas/ownership.py`): "another agent already
   owns this" is now answered from `agent_work_assignments` + `agent_scope_leases` with the
   existing directory-boundary rule. This is the OpenDots/OpenHands idea reduced to what
   LifeAI already has, without a new permission system.
2. **Forgotten-requirement queue** (`traceability.forgotten_requirements`): approved or
   proposed intent with no implementation evidence becomes a review item, never a truth
   claim. This is the Agno/OpenHands "state must be evidenced" idea applied to the atlas's
   existing trace model.

Everything else above is a proposal with an explicit owner/blocker, or a deliberate reject.

## 11. Research limits

- Public docs and blog posts describe intent and documented behaviour; they are not a
  benchmark. No competitor was run against LifeAI's corpus.
- Version drift is real: several sources are from 2026 and describe fast-moving projects.
- "LifeAI already has" means the module exists in this checkout and is referenced by the
  system map; it does not prove production behaviour. The atlas deliberately keeps
  `reviewed`/`integrated`/`activated`/`observed` unknown for that reason.
