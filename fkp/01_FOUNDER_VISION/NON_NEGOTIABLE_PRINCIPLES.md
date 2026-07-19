# Non-Negotiable Principles
**Sources:** `NON_NEGOTIABLE_RULES.md` (Life_Dev_Platform_Claude_Package), `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` (MainAI_Conversation_Knowledge_Pack_2026-07-19), `LIFE_OS_CONSTITUTION.md`
**Carried forward unchanged from FKP v1.0** — stack-agnostic, binding regardless of which codebase implements it.
**Status:** Binding. Cannot be overridden by any AI session or agent.

---

## Absolute prohibitions for all agents

1. No plaintext passwords, tokens, SMTP credentials, database URLs, or Redis URLs in chat, Git, or logs — ever.
2. No paid services or subscriptions without explicit founder approval.
3. No production deployment without founder approval and rollback plan.
4. No mode, personality, or role may increase an agent's authorization beyond its granted scope.
5. No agent may silently become MainAI/FounderAI or claim its authority.
6. No model may claim access to a page, file, network, or source it did not actually inspect.
7. No AI suggestion may be logged without rationale.
8. No unsupported critical conclusion may be published as fact.
9. No deletion of raw originals during knowledge consolidation.
10. No restart or rewrite of working modules without documented technical evidence.
11. No deployment, spending, publication, secret exposure, or irreversible action without explicit approval.
12. No fabrication of certainty where genuine uncertainty exists.
13. No cross-user memory leakage — all context is strictly user-and-conversation-isolated.

## Founder approval gates
Founder approval is required before:
- Spending or subscriptions
- Production deployment
- Irreversible database migration
- Publication or external communication
- New external integration with sensitive data
- Permission escalation for any agent
- Deletion of important data
- Changing the project constitution
- Activating autonomous browser actions involving accounts, money, communication, or legal consent
- Launching public registration or future Life City onboarding
- **Merging `claude/founder-only-launch` into the deploy branch, or triggering any Render deploy** — added in FKP v1.1 as a concrete instance of the "production deployment" gate above, since this is the actual next irreversible-adjacent step pending as of 2026-07-19 (see `06_PROJECT_STATUS/CURRENT_STATUS.md`).

## Technical non-negotiables
- Continue from existing repository; do not restart
- Split large changes into bounded, verifiable steps
- Code and identifiers in English; explanations may be Swedish
- Security designed beyond minimum legal compliance
- Trust Engine must stop unsupported critical conclusions
- User controls AI personalization, confidence, initiative, and memory
- AI should be optional where deterministic software can perform the task

## Conversation context non-negotiables (Conversation Context Resolver — binding standard)
Source: in-conversation directive, 2026-07-19
- Current user message has highest priority
- Last 3–5 messages retained verbatim with roles and timestamps
- Last 15 messages form a time-aware conversation timeline
- Explicit corrections supersede older beliefs immediately
- System must understand elliptical expressions: `nu då`, `nästa`, `den`, `han`, `lägg till i förra meddelandet`
- AI responses carry enough semantic context to function as portable handovers
- Reconstructed context must never be presented as an exact user quotation
- Context is strictly user-and-conversation-isolated with RLS — no cross-conversation leakage

**See also `08_HANDOVER/AGENT_CONTEXT_RULES.md` §"H-01 amendment"** for how this list's stop-and-ask instinct is deliberately narrowed by the founder's explicit "work on independently, don't stop for choices" instruction of 2026-07-19.
