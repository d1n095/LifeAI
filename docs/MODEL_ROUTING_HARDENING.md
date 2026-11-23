# PR #463 — model routing hardening and verification

This is builder evidence, not independent approval. `first_cycle_go=false`.
Baseline: `69dc5ea5a9cfc750303ea1a9b335c78612bc8f39`; reviewed prior head: `e884dbd24341ff2ec84b27171c5d8ed6b1c7aceb`.

## Corrections

- Latest exact provider/model/role verification must be OK within TTL, with no future timestamp. Newer failures revoke older successes. Equal timestamps prefer failure. Probe start timestamps prevent an older slow success superseding a newer completed failure. The ingest cache uses the same exact-model lookup.
- Controlled chat rechecks eligibility before each attempt, requires owner context for egress, and only uses fallback candidates satisfying the same caller policy. Explicit manual model selection never silently substitutes another model. Existing legacy callers without a selection policy retain their prior behavior and tracked egress gaps (#456).
- Founder configuration writes serialize per role with a PostgreSQL transaction advisory lock. Configuration and its audit row commit atomically. Verification responses identify their own invocation/model rather than querying an unrelated concurrent result.
- The established Alpha models `qwen2.5:0.5b` and `all-minilm` are catalogued. No runtime context width is invented. Admin model choices come from the catalog per role, and verification/activation send the same exact selection, including Z.ai.
- Vendor-declared capabilities are distinct from supported transport capabilities. Current text-only Message/ChatResult cannot promise tools, vision or structured JSON. Such required capabilities fail closed. Z.ai rejects unsupported options before network I/O.
- Z.ai validates nonempty text and nonnegative integer usage, classifies protocol/HTTP failures with fixed messages, uses header-only credentials, and conservatively reports possible external effects. Only the supported `https://api.z.ai/api/paas/v4` and `/api/coding/paas/v4` endpoints may receive credentials; redirects and environment proxies are disabled. Arbitrary proxy/custom origins are intentionally unsupported.
- Inherited prices have no fabricated capture date. A ceiling cannot pass with an unknown/stale/future snapshot date; unknown prices remain unknown. Input context checks reserve estimated output space. Local runtime context is unknown until independently measured/configured, regardless of the vendor's maximum. Prices remain estimates, not billing guarantees.

## CI and dependencies

Reused #458 runner commit `646f595452c2b43c2048ea86a91d98b154eafe88` and package/lock updates from `2ac286d8fb4cf60c6b19ffc61d488ad91b8c6a3c` without copying its added advisory exceptions. `next`/`eslint-config-next` are 16.4.6 and `sharp` is patched to 0.35.5 and PostCSS to 8.5.33. No major tooling migration or `npm audit fix --force`.

Current local audit: **0 high, 0 critical, 4 moderate**. The existing advisory allowlist is unchanged. Audit registry/network/incomplete-report errors now fail closed, with three Node tests. Both Playwright E2E jobs and the existing browser-dependent combined-container job use ubuntu-24.04; application tests/security gates are retained. The new browser regression is included in the full-stack job, with real authentication and intercepted provider endpoints so it cannot activate a paid provider.

Reproduce in a disposable Cloud environment (Python 3.12, Node 22, PostgreSQL/pgvector 16, Redis 7):

```bash
cd backend
pip install -r requirements-dev.txt
# Set DATABASE_URL to a disposable test database (fixture creates/drops it),
# APP_DATABASE_URL to its restricted runtime role, REDIS_URL to a disposable Redis DB.
python -m pytest tests/backend/providers/ tests/backend/mainai/test_egress_policy.py \
  tests/backend/mainai/test_provider_spend_authorization.py tests/backend/test_config_contract.py \
  tests/security/ tests/account/ -q
cd ../frontend
npm ci
node --test scripts/check-npm-audit.test.js
node scripts/check-npm-audit.js
npx tsc --noEmit
npx eslint .
npm run build
```

The authoritative full CI workflow is `.github/workflows/ci.yml`; exact commit and job outcomes are recorded in the PR description after completion. Local partial runs are not represented as a full backend or browser pass.

## Integration boundaries and remaining risks

- #457 (`89140ba`) overlaps verification.py. Preserve its separate local preflight timeout and installed-model handling while retaining this branch's exact-model/latest-result semantics. No #457 code or branch is modified.
- #456 (`612b4f9bf0ac115e7b35692a137b66424a6f874a`) addresses legacy missing-owner callers; this branch does not duplicate those edits. New controlled-routing calls cannot use the missing-owner bypass. Owner identity still must come from authenticated/leased callers, not model output.
- R12 ref `zai/cognitive-core-r12-release-cc4d2804` at `cc4d28042a25156b95b817393a65d58c6ade66cd` conflicts in registry.py and the add/add zai_provider.py. Its CLI evaluation adapter and synthetic embedding must remain isolated from this HTTP chat adapter. Its ModelGateRunner overwrites then pops global `_PROVIDERS['zai']`; that lifecycle must restore the prior registry entry on integration. No synthetic embeddings may enter the production index.
- The Founder supplied R15 checkpoint `transfer/freebuff-r15-20261119` at `16a6bd301a8a389e7648b8a3c1cdd80a3d115662`. It is now fetched and inspected read-only; no merge or branch modification was performed. Its last commit changes 29 files on parent `7d761c12cba06d208b631b1f744028cd31642f39`; its full inherited integration tree differs from our common base `69dc5ea...` in 317 files, so the 29-file checkpoint is not the entire integration delta. See the exact compatibility assessment below.
- Selection is not egress/spend/execution/merge/deploy authority. Spend grant enforcement remains in existing provider-planning/worker boundaries; there is no new universal billing gateway. Legacy callers and deployment-level outbound network enforcement remain separately scoped.
- Runtime quotas, service latency, mutable alias identity and actual billing prices have not been measured. Do not present static values or a ping PASS as guarantees. No latency-based routing or invented quota controller was added.
- Verification is a preflight snapshot: a remote outage or credential change can still happen after validation. Synchronous per-attempt rechecks reduce stale local decisions; they cannot atomically lock an external provider's state.
- No migration, production secret change, provider activation, merge or deployment is authorized by these changes. Independent security acceptance and exact combined-tree integration evidence remain required.

Remaining moderate advisories are the build-time `glob`/`minimatch` chain (propagated through sucrase/tailwindcss). Patches require crossing the current glob/minimatch major versions; no untested override is forced. Untrusted glob inputs are not accepted by a production route. This is residual dependency risk, not an assertion of zero vulnerabilities. PostCSS's sourceMappingURL advisory was fixed with a same-series 8.5.33 update.

After taking newer patched versions, a synthetic merge with #458 conflicts in package-lock.json. Regenerate that lock from the combined package.json while retaining sharp/PostCSS patches and the unchanged advisory allowlist; do not take the older lockfile wholesale.

Cloud validation before the final dependency/test checkpoint: **277 passed** across provider, egress, spend, config, RLS/security and account suites; **47 passed** in the final dedicated hardening suite (includes authenticated other-account/fake-Founder-role rejection). TypeScript, ESLint, production Next build and the three Node audit regressions passed. These are overlapping runs and must not be added together as a unique-test total. Full exact-head GitHub Actions status is recorded in the PR description.

## R15 compatibility assessment — exact checkpoint, no merge

Ref: `transfer/freebuff-r15-20261119` @ `16a6bd301a8a389e7648b8a3c1cdd80a3d115662`.

1. **Model identity:** `docs/FOUNDER_ALPHA_R15_STATUS.md` specifies Ollama `gemma3:4b` plus `nomic-embed-text`. `gemma3:4b` is now an explicit chat catalog entry and covered by the same fresh exact-model selection regression as the earlier Alpha models. Context remains unknown and its catalog entry does not certify the checkpoint's benchmark/quality claims. No weights were pulled and no local model was activated.
2. **Verification contract:** R15 inherits #457's `verification_timeout_seconds` and local cold-start timeout (`config.py:202`, 120 seconds), plus last-known-model default handling. Preserve these together with #463's exact-model cache, latest-failure invalidation, start-order timestamps and explicit model API argument. Taking either file wholesale would lose part of the contract. No duplicate verifier was built here.
3. **Z.ai collision — BLOCKER for combined activation:** R15 still carries the CLI/evaluation adapter in `providers/zai_provider.py`, with synthetic embeddings. `mainai_evals/model_gate.py:88–110` still overwrites then removes `_PROVIDERS['zai']`. #463 adds a permanent HTTP adapter under that key. Separate evaluation identity/imports and restore the prior registration during integration; never substitute synthetic embeddings for the production embedding provider. A combined-tree/model-gate test is required before activation.
4. **Billing — BLOCKER for treating reservations as guaranteed ceilings:** R15 `routers/chat.py:290–296` estimates tokens as characters/4 + 1024 and calls `estimate_reservation_cost` without selecting a provider/model. `billing/reservation.py:18–31` then uses the configured unknown-price fallback, rather than a proven provider bound. The chat chain can make multiple billed attempts and `chat.py:311–315` releases the reservation on total failure although an HTTP failure may have left the process. #463 conservatively marks Z.ai ambiguous effects, but R15 does not consume that signal here. Review provider-specific per-attempt reservation/settlement and failure accounting on Freebuff's lane; do not claim that selection or this PR repairs R15 billing. Existing `provider_spend` and R15's billing layer must not grant authority to one another implicitly.
5. **Memory/status/execution:** R15's checkpoint adds read-only project/GitHub status and modifies planner/executor/verification while retaining the canonical MainAI execution seam. #463 does not write goals, approvals, dispatch state or project status, and changes no migrations. The existing User/owner context and RLS must be preserved when composing the inherited schema at R15's reported head 0089. No migration numbers are inferred or changed here.
6. **Acceptance limit:** R15's document labels its own release NO-GO and reports missing backup/live-GitHub/CLI evidence. Those are checkpoint reports, not tests rerun by this task. Full compatibility and release readiness remain **FAIL / not accepted** until Freebuff publishes the reviewed combined candidate and exact-tree tests. Its ongoing branch remains untouched.

The first exact-head browser run on `d2cf1117` found a test setup race: `loginViaUi` clicks Submit but does not await authenticated navigation. The failure artifact showed the login form, not the admin page. The regression now waits for the same dashboard redirect as the existing shell E2E tests before navigating to admin; its model and activation assertions are unchanged.

After correcting the authenticated-navigation setup, `e2e/model-routing.spec.ts` passed locally in Cloud with real PostgreSQL, backend and Chromium (1 passed, 5.3s). The latest dedicated backend hardening run, including R15 Gemma model identity, passed 47/47. Provider endpoints in the browser regression were intercepted; no paid provider was invoked or activated.
