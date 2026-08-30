# MainAI future-prep — minimum growth risks (composed era)

Design only the minimum necessary fixes now. Do not overbuild.

| Risk | Where | Mitigated already? | Min fix now | Defer |
|---|---|---|---|---|
| Continuity note growth | `founder_memory_notes` with `mainai_executive_continuity_v1` | Append-only by design | Cap retained checkpoints per session (e.g. keep last 20) | Full archival pipeline |
| WorkCandidate backlog | `work_candidates` executive-scan rows | Bounds via `ExecutiveScanBounds` | Stable idempotency on owner+horizon+title (not session) + surface unreviewed age | Priority-aware claim |
| Workforce agent proliferation | `workforce_agent_profiles` | `MAX_ACTIVE_OR_PROBATION_AGENTS`, ROI hire gate | Keep caps; retire unused | Full org planner |
| Performance rollup staleness | `workforce_performance_rollups` | Evidence-weighted scores | Decay factor on old evidence | Full trust model |
| Capability confidence staleness | `capability_records` | Explicit status only | Time-based degrade of `verified_available` → `unknown` | Predictive self-model |
| Assumption backlog | `life_problem_assumptions` | Executive assumption scan surfaces them | Founder review queue UX | Auto-invalidation cascade |
| Lesson conflict pairs | `engineering_lessons` | Deterministic candidate pairing | Do not auto-AI-judge at scale | Async conflict worker |
| Provider cost creep | spend + workforce cost | Dry-run + ceilings | Keep invoke disabled until gates | Continuous cost governor |

## Absolute

FUTURE PLAN ≠ AUTHORITY · PERFORMANCE ≠ AUTHORITY · TRUST SCORE ≠ AUTHORITY.
