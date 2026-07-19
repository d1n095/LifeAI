# Reading Order
**For:** Any new agent or session starting work on this project.

---

## If you have 5 minutes (orientation)
1. `08_HANDOVER/SESSION_BOOTSTRAP.md` — what exists, what works, what the priority is
2. `06_PROJECT_STATUS/CURRENT_STATUS.md` — verified current status
3. `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md` — what to do next, in order

## If you are starting implementation work
1. `08_HANDOVER/SESSION_BOOTSTRAP.md`
2. `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md` — verify against the actual repo before trusting it
3. `03_ARCHITECTURE/BUILT_VS_DESIGNED.md` — do not rebuild what exists
4. `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md`
5. Then: the relevant section for your specific task

## If you are working on the knowledge/memory model
1. `08_HANDOVER/SESSION_BOOTSTRAP.md`
2. `03_ARCHITECTURE/TARGET_ARCHITECTURE.md` — read the stack-translation caveat first
3. `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md` Steps 4–5 (knowledge import, project memory)
4. `02_DECISIONS/DECISION_REGISTER.md` D-17/D-18/D-19

## If you are working on AI orchestration
1. `03_ARCHITECTURE/AI_RESOURCE_ORCHESTRATION.md`
2. `03_ARCHITECTURE/ADAPTIVE_WORK_ORCHESTRATION.md`
3. `08_HANDOVER/AGENT_CONTEXT_RULES.md`
4. `04_PRODUCT_AND_MODULES/MODULE_REGISTER.md`

## If you are working on security or the four open founder decisions
1. `05_SECURITY_AND_TRUST/SECURITY_REQUIREMENTS.md`
2. `01_FOUNDER_VISION/NON_NEGOTIABLE_PRINCIPLES.md`
3. `02_DECISIONS/OD_02_03_04_17_ANALYSIS.md` — alternatives and recommendations for OD-02/03/04/17, not decisions

## If you need to understand the vision
1. `01_FOUNDER_VISION/FOUNDER_CONSTITUTION.md`
2. `01_FOUNDER_VISION/MASTER_PRODUCT_VISION.md`
3. `01_FOUNDER_VISION/NON_NEGOTIABLE_PRINCIPLES.md`
4. `01_FOUNDER_VISION/DOMAIN_AND_REQUIREMENT_MAP.md`

## If you're picking up right where FKP v1.0 left off
1. `00_MASTER_INDEX/CHANGELOG_FROM_V1.md` — what changed and why
2. `00_MASTER_INDEX/FKP_V1_AUDIT.md` — the audit that drove those changes
3. `07_CONFLICTS_AND_GAPS/CONFLICT_REGISTER.md` — what's resolved, what's still open

## Never do
- Skip `07_CONFLICTS_AND_GAPS/CONFLICT_REGISTER.md` before starting implementation
- Assume answers to items in `02_DECISIONS/OPEN_DECISIONS.md`, including OD-02/03/04/17 — analysis exists, decisions do not
- Use `07_CONFLICTS_AND_GAPS/STALE_INFORMATION.md` entries, or anything in `10_HISTORICAL_DOMAIN_MATERIAL/`, as current LifeAI fact
- Present reconstructed context as the user's exact words
- Proceed past a trust/approval gate without required approval — see `01_FOUNDER_VISION/NON_NEGOTIABLE_PRINCIPLES.md`, and note the H-01 amendment in `08_HANDOVER/AGENT_CONTEXT_RULES.md` only narrows unnecessary stop-and-ask, it does not remove these gates
