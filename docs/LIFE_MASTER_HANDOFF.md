# Life (MainAI) — MASTER HANDOFF

**Read this first in any new conversation or agent session.** It is a *pointer index*, not a second
knowledge store. When this disagrees with an authoritative doc, the authoritative doc wins — fix this file.
Keep it short. Do not grow it into a parallel plan. `first_cycle_go=false` everywhere.

_Last updated: 2026-11-01 by Claude Opus 4.8 (independent examiner). Base: `claude/det-kommer-mer-879lcm`
@ `64c07d5b0` (= origin default tip)._

## 0. Standing invariants (never violate without explicit Founder say-so)
No merge · no deploy · no DNS/STRATO change · no paid/live provider calls · no Recall activation · no
autonomous GO · do not modify frozen candidates · do not overwrite another agent's branch · independent
reviewer required (no self-certification) · `UNKNOWN != VERIFIED` · `REPO-WRITE ≠ PROVIDER-SPEND ≠ EGRESS`.

## 1. Authoritative documents (read these, don't re-summarize them here)
- Vision / memory architecture: `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` (8-layer model, §0 blocker, §2 gaps)
- Readiness ladder: `docs/MAINAI_STARTUP_READINESS.md` (`BLOCKED→SAFE_INTERNAL_RUN→LOW_RISK_PROVIDER→SERIOUS_AUTONOMOUS`)
- Autonomy chain: `docs/MAINAI_V1_GOAL_TO_AUTONOMY.md`; blockers: `docs/FIRST_AUTONOMOUS_TASK_BLOCKER_MAP.md`
- Local-first: `docs/MAINAI_LOCAL_INTELLIGENCE_SCHOOL.md`; vault/egress: `docs/LIFE_VAULT_EGRESS_CONTROL.md`
- Security deferrals: `docs/SECURITY_BLOCKERS.md`; living branch map: `docs/BRANCH_REGISTRY.md`
- **This recon's full report:** `docs/LIFE_TAKEOVER_READINESS_RECON.md` (A–I evidence)
- Governance for every session: repo `CLAUDE.md` (isolate/verify/coordinate/review; merge-rule).

## 2. Verified facts (this session, evidence-backed)
- Platform is mature: 44 backend packages, 55 models, 68 migrations, 198 backend test files, 62 RLS policies.
- Memory layers 1–3 built (immutable originals, source, claims+grounding); 4–8 partial/missing.
- Founder→worker autonomy chain substantially built; BUT first self-improve run (#205) used a disposable
  mirror, not the real checkout — real-checkout supervised cycle is **unproven**.
- Local coding = **framework only** (`mainai_school/`), not production-ready.
- **Egress classification missing**; chat sends raw content to providers unfiltered.
- **Historical ChatGPT vision is NOT in the repo** (0 doc hits for business/finance/comms/multi-project);
  no ChatGPT importer; conversations not yet a first-class memory source. Needs Founder-authorized export.

## 3. Exact PR / commit state (as of 2026-11-01)
- Default branch `claude/det-kommer-mer-879lcm` @ `64c07d5b0`; **no open PR merged into it yet.**
- Independent-exam verdicts (this session), each posted on its PR:
  - **#319** `397672d84` selector/delegation — **PASS**
  - **#320** `6c1240d8` crash/resume (independent lineage) — **PASS**
  - **#321** `e1f649a9` RT3 cycle/webhook (single mig head `0100`) — **PASS**
  - **#322** `a7c1ffa3` coordination fencing (single mig head `0101`) — **exam completing; verdict pending full suite**
- **#324** `306b980f4` `codex/coordination-ledger-dedup-takeover` (parent `a7c1ffa3`=#322; mig `0102`) —
  **ACTIVE Codex work** (CC3/CC4/AR6). Do not duplicate/modify; sequence after #322.
- Claude lanes: **#323** (npm-audit baseline + CI Playwright `ubuntu-24.04` pin) CI green; #316/#317/#318
  (passkeys/PWA) code CI green, npm-audit clears once #323 merges to base.
- Deep cursor/* stack #285–#315 (coordination/provider/large-file/founder-workflow/first-cycle); unmerged.

## 4. Open questions for the Founder (block progress; see report §H)
1. Authorized ChatGPT export available? 2. Egress stance for ingestion (local-only vs classified subset)?
3. Is the business/financial/multi-project layer in-scope for Life's memory now? 4. First takeover demo =
comprehension (ingest an authorized export) or code (bounded real-checkout cycle)?

## 5. Current assignments / ownership
- **Claude (this lane):** independent examinations #319–#322 (+#320); this reconnaissance + handoff.
- **Codex:** #319/#321/#322 builder + **#324 active** (coordination/takeover fencing). Do not overlap.
- **Cursor:** autonomy-activation + first-cycle + messaging/GitHub-intake lanes (#285–#315, #300–#304).

## 6. Next safe action
Publish this handoff + `LIFE_TAKEOVER_READINESS_RECON.md` on an isolated docs branch with a **draft** PR
(after #322's verdict). Then await Founder answers to §4 before any build. Recommended first milestone:
**M1 (reconcile `BRANCH_REGISTRY` ↔ real PR graph + per-doc status)** — zero-risk, highest leverage — and
pose the §4 questions. No code until the Founder picks the first-takeover demo (report §I: Option A
comprehension-first recommended).
