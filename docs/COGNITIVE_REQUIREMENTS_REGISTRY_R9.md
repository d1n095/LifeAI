# Cognitive Requirements Registry — Round 9 (pre-start audit, cognitive domain)

**Lane:** Z.ai · **Branch:** `zai/alpha-runtime-reliability` · **Baseline:** `d6a3111f` → **HEAD this round:** `4dff86c8`
**Purpose:** the assigned cognitive-domain half of the overall pre-start requirements audit. For each capability: what is genuinely implemented, whether it is reachable through a production path, what tests prove it, whether it is startup-critical, and what remains. Findings from the audit (dead paths, duplicates, contradictions) are stated honestly, with this round's fixes.

**Verification status at HEAD:** backend 2646+ (pre-R9 run) plus R9's 26 new integration tests; full chunked suite re-run at the end of this round (see R9 report).

---

## 1. Memory & continuity

| | |
|---|---|
| Implemented | `app/founder_memory_signals/service.py` (candidate signals → founder promote/dismiss), conversations/messages persistence, `app/active_context/`, lessons as durable engineering memory, epistemic-state audit trail |
| Production path | chat resolver signals → candidate signals; R9: a conclusion that CHANGES (contradicts edge moving a ladder verdict) records a `correction_candidate` with provenance — idempotent, restart-persistent, dismissable through the existing contract |
| Tests | `test_cognitive_cycle_continuous.py` (remember/dismiss/persist), chat signal suites |
| Startup-critical | **YES — implemented.** The signal producer ≠ truth-writer boundary holds (schema-constrained statuses) |
| Remaining | Auto-promotion policy for recurring corrections (founder-decision territory, not implemented on purpose) |

## 2. Research intelligence

| | |
|---|---|
| Implemented | `app/rag/retrieve.py` (owner-scoped, egress-gated), `evidence_metadata.py` (closed-vocabulary producer, 17 declared keys), `evidence_quality.py` (11 grading axes incl. sample size, statistical reporting, replication, follow-up; ladder caps design-driven), topic landscapes (R9), body-of-evidence landscape |
| Production path | Library stamp endpoint → detail view grades; chat/workbench `EVIDENSSTATUS` + topic lines; cross-country/cross-design disagreement flags from DECLARED edges |
| Tests | `test_evidence_quality.py`, `test_evidence_metadata_integration.py`, `test_topic_landscape.py` |
| Startup-critical | **YES — implemented.** A funding conflict is scrutiny, not rejection (floors at `low`, travels as a caveat — asserted) |
| Remaining | **Cross-LANGUAGE disagreement detection is NOT implemented** (settings are free text; no language normalization) — registry item, not a startup blocker. Question-scoped (vs owner-scoped) landscape clustering — later enhancement |

## 3. Epistemic reasoning

| | |
|---|---|
| Implemented | `app/rag/epistemics.py`: the assumption→hypothesis→verified_fact ladder with the conflict override, per-claim classify + SQL mirror, second-order consequences, cross-domain overlaps |
| Production path | Library detail per-claim `epistemic_status`; chat/workbench rollup line; claim graph endpoints; graph context in answer paths (R9) |
| Tests | `test_epistemics_integration.py` + graph suites |
| Startup-critical | **YES — implemented.** Unsupported certainty stays `assumption` regardless of metadata (asserted); contradictions move verdicts, never destroy records |
| Remaining | The ladder is rule-based by design (model-independent); no LLM-judged nuance layer (deliberate) |

## 4. Knowledge provenance

| | |
|---|---|
| Implemented | claim→chunk→source grounding, `claim_evidence_metadata` (declared_via, updated_at, rejections), evidence-chain trace endpoint, `engineering_lesson_plan_decisions` provenance edge, `epistemic_shift` audit rows |
| Production path | `GET /claims/{id}/evidence-chain`; lesson decision rows written at plan creation; audit trail on every shift |
| Tests | `test_claim_graph_integration.py`, hardening suite (rollback atomicity) |
| Startup-critical | **YES — implemented.** Every conclusion names its evidence; every ranking decision names its counts |
| Remaining | Citation of source PAGES (sub-chunk offsets) in the chain view — cosmetic |

## 5. Cross-domain inference

| | |
|---|---|
| Implemented | `app/rag/claim_graph.py` (writer, trace, shift detection) + library endpoints + `app/rag/graph_instructions.py` (R9: consequences/overlaps/topic in chat+workbench answer paths) |
| Production path | Founder declares edges via `POST /claims/{id}/relationships`; consequences propagate to dependent documents (claim-level edges included since R9); overlaps discovered across projects; the model SEES the graph state every turn |
| Tests | `test_graph_prompt_integration.py` (output-change proofs), `test_claim_graph_integration.py` |
| Startup-critical | **YES — implemented THIS ROUND** (was the audit's biggest dead spot: R8's endpoints existed but nothing in the answer path invoked them) |
| Remaining | Cross-language overlap matching is whitespace-normalized only |

## 6. Planning & decision support

| | |
|---|---|
| Implemented | `app/mainai_execution/planner.py` (deterministic create_plan: validation, dependency graph, versioning) + measured lesson ranking (R8) + decision provenance |
| Production path | `create_plan` ranks candidates by standing; proven first; failed-majority/unusable excluded with reasons; selection audit block on the created event |
| Tests | `test_lesson_selection_integration.py`, `test_mainai_execution_planner.py` |
| Startup-critical | **YES — implemented.** Unverified lessons are applied as FLAGGED hypotheses, never presented as established rules |
| Remaining | **No LLM planner exists** — "planner prompt policy" is therefore minimal by design; the standing block informs chat/workbench only. A generative planner is a founder-approval architecture decision |

## 7. Self-correction

| | |
|---|---|
| Implemented | lessons + guard observations + effectiveness rollup + selection layer + recurring-failure clusters (root causes, guard targets, corrective actions) + conflict-candidate machinery + R9 loop-integrity harness |
| Production path | finalize writer → observations → standing → next plan's ordering; clusters surface in the prompt layer; exclusions recomputed every pass (no permanent suppression) |
| Tests | `test_reasoning_loop_integrity.py` (read-only selection, subset invariant, cluster linkage), R8 selection suite |
| Startup-critical | **YES — implemented.** The ranking structurally cannot reinforce itself (writes nothing) or fabricate success (counts only) |
| Remaining | Cluster RESOLUTION is founder/async-tick only (correct boundary); no auto-lessons from model reasoning |

## 8. Agent learning

| | |
|---|---|
| Implemented | problem_learning (signature normalization, recurrence), correction_intelligence (dedup vs existing lessons), lesson_from_verification (finalize writer), candidate signals |
| Production path | worker paths (recovery_inspector → applicable lessons; task failure → problem learning); all SIGNAL producers |
| Tests | problem_learning suites, lesson_from_verification suite |
| Startup-critical | **YES — implemented.** |
| Remaining | Cross-task failure attribution to shared root causes is DETECTION-only (correct); no auto-generated lessons from LLM output (deliberate) |

## 9. User-specific knowledge isolation

| | |
|---|---|
| Implemented | RLS FORCE on all cognitive tables + explicit owner filters + owner-scoped retrieval + founder-only routers |
| Tests | isolation assertions in EVERY new suite (standing, landscape, graph, retrieval leakage, prompt layer) |
| Startup-critical | **YES — implemented.** A lesson's standing, a topic's landscape, a graph's consequences, and even the assembled prompt provably never cross owners |
| Remaining | None known in the cognitive domain |

## 10. Model-independent deterministic reasoning

| | |
|---|---|
| Implemented | `evidence_quality.py`, `epistemics.py`, `trust.py`, `claim_graph.py`, `lesson_selection.py` are pure functions/SQL — same inputs always yield the same outputs; the LLM only renders Swedish instruction lines from those outputs |
| Tests | determinism asserted implicitly by every pure-function suite; fail-open degradation asserted for every prompt integration |
| Startup-critical | **YES — implemented.** This is the system's strongest property: conclusions, grades, rankings and shifts are reproducible and auditable without any model |
| Remaining | The injected landscape/instruction LINES could grow a golden-file test (cheap, additive) |

---

## Audit findings and actions

1. **Dead spot (fixed, R9 M3):** R8's graph endpoints had no caller in the answer path — an imported function, not a capability. `format_graph_context_instructions` now runs per turn for the retrieved sources; output-change tests prove the delta. En route, the workbench call raised a NameError that fail-open silently swallowed — exactly the failure mode the tests exist to catch.
2. **Dead seam (fixed, R8 M3):** the epistemic rollup was imported into chat/workbench without ever being appended.
3. **Dual conflict models (documented, NOT unified):** `detect_claim_conflicts` (source-level, retrieval trust) and claim-level `contradicts` edges (ladder + graph) coexist. They answer different questions (whole-source vs per-claim) and both are tested; unifying them is an architecture decision reserved for the founder.
4. **Legacy read path (documented):** `recovery_inspector.py` still uses unranked `lookup_lessons`. Changing recovery inspection behavior is a recovery-policy change — flagged, not changed.
5. **Signal vocabulary (reused, not extended):** migration 0053's closed enum gained no new kinds; the epistemic correction rides `correction_candidate` with full provenance. Schema-policy changes stay with the founder.
6. **No duplicate implementations found** in the cognitive domain; the landscape summarizer (R8 owner-wide) and topic landscape (R9) are intentionally two scopes of one aggregation core.
7. **Startup-critical gaps implemented this round:** topic-scoped landscapes (M2), loop-integrity + cluster→action linkage + feedback surface (M1), graph-in-answer-path (M3), untrusted-content framing (M4), memory bridge + measured read-path costs (M6). Everything else found is registered above as a later enhancement, none blocking the first private startup.
