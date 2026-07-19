# Complete Domain and Requirement Map
**Source:** `03_COMPLETE_DOMAIN_AND_REQUIREMENT_MAP.md`, part of the `MainAI_Conversation_Knowledge_Pack_2026-07-19` ("samtalsregistret" — the external conversation-record package the founder asked to be integrated into FKP v1.1). Carried forward with no content changes; this is forward-looking domain/requirement scope, not a claim about current LifeAI code state.
**Status:** DESIGNED / vision-level. Separates near-term foundation work from long-term product ambition. None of the "Deferred" sections are built or scheduled.
**Relation to other FKP v1.1 documents:** §1–§5 and §13 directly inform `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md`. §11 relates to the historical/domain material in `10_HISTORICAL_DOMAIN_MATERIAL/`. §6 relates to `03_ARCHITECTURE/AI_RESOURCE_ORCHESTRATION.md`.

---

This map separates immediate foundation requirements from later product ambitions.

## 1. Founder Control Plane

Required:

- founder-only authentication;
- MainAI root identity;
- Founder Vault;
- approval queue;
- decision ledger;
- task and agent overview;
- cost and provider overview;
- security and audit overview;
- rollback and incident controls.

## 2. Founder UserAI and future UserAI

Required architecture:

- private identity and memory scope;
- personal preferences and explanation style;
- routines, goals, calendar, health, mood-related self-reports, and life context;
- no medical diagnosis from casual signals;
- explicit delegation to specialist agents;
- explicit, audited delegation to MainAI when root work is necessary;
- future per-user isolation, export, correction, and deletion.

## 3. Context, Meaning, and Intent

- current message priority;
- short verbatim window and longer timeline;
- topic stack;
- reference resolution;
- corrections and supersession;
- temporal and causal reasoning;
- active page/UI state;
- Swedish slang, spelling, ambiguity, and indirect meaning;
- language-neutral semantic representation for translation;
- distinction among request, idea, decision, correction, question, status, and withdrawal;
- context-carrying responses and reverse reconstruction.

## 4. Memory and Knowledge

- working, episodic, semantic, and structured memory;
- user, founder, project, organization, and shared scopes;
- provenance and source chains;
- immutable versions;
- aging and relevance;
- conflict detection and resolution;
- human approval proportional to impact;
- retention, export, deletion, and legal basis;
- Founder Knowledge Pack with originals and checksums.

## 5. Multi-Agent Orchestration

- capability registry;
- agent diagnostic profiles;
- task graph and microsteps;
- routing by quality, context, price, quota, tools, latency, and reliability;
- checkpoints and evidence-based completion;
- standardized handovers;
- modes: crawl, stair, interval, sprint, and safe stop;
- resumption when an agent becomes available again;
- local-model background work where safe.

## 6. AI Providers and Local Models

- provider gateway;
- direct foundational-provider APIs where used;
- fallback and circuit breaking;
- normalized usage and cost reporting;
- budget and concurrency controls;
- model/prompt/version registry;
- evaluation before routing production work;
- long-term local-first path with quality-controlled learning material.

## 7. Trust and Investigative Intelligence

- per-claim evidence graph;
- source quality and source independence;
- raw claim vs interpretation vs hypothesis vs verified conclusion;
- contradiction and incentive analysis;
- timeline and relationship map;
- continuous monitoring within legal and budget limits;
- strong safeguards against fabricated conspiracies or unsupported accusations;
- visible uncertainty and missing evidence.

## 8. LifeWeb and Browser Assistance

Deferred but designed requirements:

- browser extension first, full browser only if justified;
- DOM and accessibility-tree understanding;
- screenshots when required;
- precise navigation based on actually inspected UI;
- record attempted, failed, and successful paths;
- same UserAI identity and explanation preferences;
- password-manager integration without plaintext password exposure to the model;
- approval for sensitive actions;
- protection against prompt injection from web content;
- page provenance, session boundaries, and private/incognito controls.

## 9. Life Library / Knowledge Studio / LifeCast

- PDF, DOCX, text, web page, image, audio, YouTube link, and uploaded video ingestion;
- transcription, OCR, visual analysis, segmentation, and metadata;
- original preservation and content rights;
- source-aware summaries and research products;
- generated podcast or audio discussion from approved materials;
- generated-product versioning and traceability;
- deletion propagation and derivative tracking.

## 10. Voice and Orb

- voice input and output;
- wake phrase `Life` when platform permissions allow;
- listening, thinking, speaking, error, quiet, and work states;
- accessibility and reduced-motion options;
- orb may later transform into a map or control surface;
- never imply that a browser web app can replace operating-system assistants without platform permissions.

## 11. Life City and Maps

Deferred vision:

- city-based onboarding/identity station;
- visual Life OS environment;
- orb-to-map transformation;
- geographically grounded streets/buildings where licensing permits;
- no unlicensed copying of Street View or proprietary map imagery;
- privacy-safe representation without reproducing identifiable people;
- investigation/evidence overlays separated from entertainment or speculation.

## 12. Life OS Functional Modules

Broader project modules mentioned across the supplied context:

- calendar, planning, reminders, alarms, templates;
- work schedules, salary, overtime, breaks, multiple workplaces;
- OCR/scanning for schedules, receipts, and payslips;
- personal finance, bills, debts, savings, forecasts, and a private vault;
- documents, notes, notifications, backup, and export;
- health, training, food, weight, steps, and coaching;
- travel, maps, transport, costs, weather, and warnings;
- organizations, permissions, workplaces, and staff;
- Life Bridge care/assistance system;
- psychology-support module requiring professional/legal safeguards;
- wish list, purchase list, completed purchases, manifestation timelines;
- community, reviews, social/event concepts;
- universal search, reporting, forms, rules, automation, and knowledge graph.

## 13. Security and Privacy

- secure cookies and session revocation;
- passkeys/MFA and recovery strategy for founder;
- RLS and ownership tests;
- secret management and rotation;
- encryption for sensitive fields and Founder Vault;
- immutable audit events;
- rate limits and abuse protection;
- dependency and supply-chain checks;
- prompt-injection defenses;
- tool sandbox and least privilege;
- incident response and emergency lockout;
- backup restoration drills.

## 14. Operations and Quality

- health and readiness checks;
- structured logs and correlation IDs;
- model, prompt, tool, and knowledge-version traceability;
- alerting and service objectives;
- feature flags and safe rollout;
- database migration safety;
- rollback procedures;
- deterministic tests plus model evaluation;
- Swedish golden conversations;
- offline/degraded mode where possible;
- accessibility testing.

## 15. Law and Governance

- GDPR and user rights;
- content/document/video rights;
- map and imagery licensing;
- email and communication consent;
- health and care regulatory boundaries;
- recording and transcription consent;
- biometric and identity requirements;
- transparency for AI decisions;
- no autonomous legal or medical determinations without qualified oversight.
