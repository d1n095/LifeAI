# Candidate Requirements — captured, not decided
**Source:** `NEW_REQUIREMENTS_CAPTURE.md`, `FKP_v1_REVIEW_OVERLAY` (the review layer the founder supplied on top of FKP v1.0, integrated into this v1.1 package per the founder's instruction). Carried forward with no content changes — the review overlay's own framing already matches FKP v1.1's status discipline (see `07_CONFLICTS_AND_GAPS/CONFLICT_REGISTER.md` §H-02 status-field correction below).
**Status:** Requirement and design **candidates**. Nothing here is automatically decided architecture. None of it is implemented.

---

## 1. Relational Intent & Initiative

The AI should understand what an utterance *does* in the conversation, not just its words.

Examples:

- "Shit, I need this" → possible requirement/memory signal.
- "Check out what I found" → offer analysis and placement in the right context.
- "This wasn't good" → identify what is being corrected and replace the older assumption.
- "Is this info that should be saved?" → assess scope, sensitivity, durability, and only ask if genuinely necessary.
- "Add it to the previous message" → edit the prior intent, don't start a new standalone task.

The system should learn how the founder expresses themselves, but memory must stay visible, correctable, exportable, and deletable. Conclusions about stress, mood, or emotion are weak supporting signals — never diagnoses or decision mandates.

## 2. Proactive multi-perspective review

For important questions, the system should be able to review the material as multiple roles:

- teacher/educator,
- product strategist,
- architect,
- designer/UX,
- developer,
- tester,
- security/privacy,
- legal/compliance (with a clear boundary: not legal advice without qualified review),
- operations,
- cost,
- data/AI,
- red-team/critic,
- user advocate,
- founder-vision.

Output should distinguish:

1. direct answer,
2. missing questions,
3. competing alternatives,
4. risks and consequences,
5. dependencies,
6. recommendation with evidence,
7. what still requires a founder decision.

The system should not flood the user; it prioritizes what affects the next step.

## 3. Design Exploration & Decision Lab

The website should eventually be able to collect documents, conversations, code, ideas, sources, and tests to:

- extract claims and requirements,
- link contradictions and overlap,
- compare similar approaches against each other,
- ask what/why/who/where/when/how/can/if,
- show what is observation, candidate, tested, recommended, and decided,
- preserve why a decision was made and what would trigger reconsideration.

This is the working method that should precede broad architecture lock-in — not a decorative future module.

## 4. Adaptive Systems Registry

Several systems need to be improvable over time: context, memory, routing, diagnostics, security rules, expert questions, UI, test suites, and cost strategy.

Candidate lifecycle:

`observe → propose → validate → sandbox → compare → recommend → approve if needed → version → monitor → rollback`

Improvements should have different approval levels:

- automatic only for low-risk, reversible internal adjustments,
- verification required for behavior and quality changes,
- founder approval required for vision, permissions, security, cost, production, and irreversible actions.

## 5. AI diagnostics

Every exact AI configuration should get a comprehensive form and behavioral test. The test should measure more than self-report:

- context and corrections,
- logical reasoning,
- hallucination/honesty,
- code and tools,
- multimodality,
- security/prompt injection,
- permission discipline,
- handover quality,
- cost awareness,
- proactivity without scope creep,
- ability to propose missing test questions.

The diagnosis should be updated when model, prompt, tools, memory, or permissions change.

## 6. Multi-agent workforce and resource balance

Claude, ChatGPT/Codex, Gemini, Kimi, Lovable, and local models should be used where their **verified** strengths fit. Quota, cost, context pressure, and availability should govern pace and handover.

Manual free chats can contribute via files and handover, but must not be described as automated API agents.

Work is broken into small, verifiable steps with checkpoints. Other safe, independent tasks should continue when one provider is temporarily exhausted.

## 7. LifeWeb and browser assistance — future candidate

The founder's UserAI should eventually be able to follow LifeWeb/browsing when the founder explicitly activates it, giving precise navigation and explanations adapted to the current context.

Requirements before implementation:

- clear active consent and a visible "AI sees the page" status,
- domain-, tab-, and time-limited access,
- no hidden monitoring,
- sensitive fields and passwords masked by default,
- password handling uses a secure vault/passkey integration — the model should not read plaintext secrets,
- audit, pause/stop, revocation, and deletion,
- legal/privacy review before broad use.

The AI is a support person. Other authorized agents/trust gates make or approve high-risk decisions; the browser component should not get its own root permission.

## 8. Founder-only MainAI and separate AI identities

- MainAI is locked to the founder and the project's top coordination role.
- Founder UserAI is the founder's personal AI, linked to the founder account but with a separate identity and permission profile.
- "Work mode" and other modes change working style, not base permission.
- Future users get their own isolated UserAI; they must never get implicit access to MainAI or the Founder Vault.

**Status in LifeAI as of v1.1:** MainAI's founder-only lock (first bullet) is now implemented and CI-verified (D-27). Founder UserAI as a *separate identity* does not exist yet — LifeAI currently has exactly one identity (the founder account) with no distinct "MainAI vs. Founder UserAI" split at the code level. See `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md` Phase 4.

## 9. GitHub/Update Center — future candidate

The website could later show prepared branches/PRs, diffs, tests, previews, risks, and rollback. An approved agent could work in a sandbox in the background and report "ready for review/merge/update."

The founder should still approve merge/deploy. The exact UI, GitHub App model, and station/city metaphor should be compared against other solutions before deciding.

## 10. Digital twin / test city — future candidate

The idea of a "backup city," Mirror City, or digital twin could represent isolated test environments, branches, simulations, and rollback. Orb-as-map, real-world-grounded streets, and agent stations are possible interfaces/metaphors.

These should be stored as design candidates. Map data, Street-View-like material, people, copyright, personal data, licenses, and security require separate investigation. No such idea should block Step 1.

## 11. Base rule for all candidates

A good idea is not automatically a decision. The system should:

1. capture the idea without losing it,
2. place it in the right domain,
3. find similar and contradicting proposals,
4. compare alternatives and dependencies,
5. test where possible,
6. recommend with uncertainty stated,
7. wait for the right decision gate,
8. preserve traceability after the decision.
