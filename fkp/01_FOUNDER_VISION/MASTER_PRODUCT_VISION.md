# Master Product Vision
**Source:** `02_MASTER_PRODUCT_VISION.md` (Life_OS_Claude_Handoff_Package), `01_EXECUTIVE_CONTEXT_AND_CURRENT_STATE.md` (MainAI_Conversation_Knowledge_Pack_2026-07-19)
**Carried forward from FKP v1.0 with one correction:** the "Step 1 — Current confirmed scope" section below described Step 1 as a plan. It is updated here to record that Step 1 has since **shipped and passed CI** on branch `claude/founder-only-launch` (commit `bf31fad`, independently re-reviewed and fixed at `e9b4b76` — see `06_PROJECT_STATUS/CURRENT_STATUS.md`). It is not yet merged to the deploy branch or deployed; that still requires explicit founder approval.
**Status:** Canonical. Extended by July 2026 conversation decisions.

---

## What Life OS is
Life OS is a modular, AI-optional operating system for human life, work, knowledge, projects, organizations, and long-term development. It is not a collection of disconnected apps. It is a shared system where:
- objects are reusable
- modules communicate through governed capabilities
- data is entered once
- AI can route intent to the correct functions
- knowledge is connected
- decisions are reviewed
- the platform learns through verified experience

## Platform components — priority order

1. **MainAI / FounderAI** — founder's root-level AI, control plane, Founder Vault, agent coordination. Strictly founder-locked.
2. **Founder UserAI** — founder's personal support AI. Handles private preferences, routines, daily assistance. Does NOT receive implicit root access. Separate from MainAI.
3. **Source Hub** — import/sync of files, exports, code, and AI conversations from multiple tools.
4. **Life OS modules** — finance/salary, work/calendar, documents/OCR, health, food, travel, goals, smart home, social, etc. Pre-existing application.
5. **Future UserAI** — isolated UserAI instances for future users, based on approved template. Never receive MainAI or Founder Vault access.
6. **Life City / GlowUp / 4ThePeople** — future e-commerce and city layer. Deferred.

## Primary audiences
Private individuals, families, employees, self-employed, small companies, large organizations, municipalities, associations, support organizations, future developers and module creators.

## Product principles
- Works without AI; AI can be enabled or disabled
- No unnecessary duplicate registration
- Modular installation and progressive complexity
- Strong user ownership of data
- Role- and permission-based organizational use
- Provider-independent AI architecture
- Long-term design horizon of decades

## Universal experience goal
A user should be able to say: "I have a meeting in two hours." / "I worked 14–21 yesterday." / "What can I cook?" / "Find this product." / "Show the document I uploaded last summer." / "Why am I more tired this week?" — and Life OS determines which modules, tools, data, and specialists are relevant.

## Step 1 — Founder-only launch: SHIPPED, awaiting merge/deploy approval
Status as of FKP v1.1 (2026-07-19), see `06_PROJECT_STATUS/CURRENT_STATUS.md` for full verification detail:

1. ✅ Exactly one founder account is provisioned on startup, keyed on a fixed non-secret UUID (`app/founder.py`), idempotent.
2. ✅ Public registration button and page removed from the frontend (`frontend/app/register/page.tsx` is a dead redirect to `/login`; `robots.ts` blocks indexing).
3. ✅ Public registration disabled server-side — `POST /api/auth/register` returns `404` when `ENVIRONMENT=production`, not merely hidden in the UI.
4. ✅ Login, logout, password reset, and session revocation (`/me`, `/logout-all`) retained on generic authenticated-session handling — deliberately **not** founder-gated, so the underlying account-security machinery stays exercised and testable.
5. ✅ All MainAI-surface routes (chat, conversations, documents, knowledge, projects, admin) require `require_founder()` — role check plus fixed-UUID identity check, defense in depth.
6. ⏳ Deploy gate: CI is green on the dedicated validation branch `claude/founder-only-launch`; merge to the deploy branch and any Render deploy still require explicit founder approval — neither has happened yet.

Everything else (Life City, public UserAI onboarding, LifeWeb, maps) must not interrupt Step 1 and has not been built.
