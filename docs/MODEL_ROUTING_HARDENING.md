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

Reused #458 runner commit `646f595452c2b43c2048ea86a91d98b154eafe88` and package/lock updates from `2ac286d8fb4cf60c6b19ffc61d488ad91b8c6a3c` without copying its added advisory exceptions. `next`/`eslint-config-next` are 16.4.6 and `sharp` is patched to 0.35.5. No major tooling migration or `npm audit fix --force`.

Current local audit: **0 high, 0 critical, 11 moderate**. The existing advisory allowlist is unchanged. Audit registry/network/incomplete-report errors now fail closed, with three Node tests. Both Playwright E2E jobs and the existing browser-dependent combined-container job use ubuntu-24.04; application tests/security gates are retained. The new browser regression is included in the full-stack job, with real authentication and intercepted provider endpoints so it cannot activate a paid provider.

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
- No identifiable R13/R14/R15 branch, exact SHA or transferable release package was available from repository refs/PR searches at baseline. Freebuff R15 compatibility is **not verified**. Required input: its exact reachable SHA or release bundle, particularly changes to providers, verification, provider_config, egress, model gates and migrations. The R15 branch is untouched.
- Selection is not egress/spend/execution/merge/deploy authority. Spend grant enforcement remains in existing provider-planning/worker boundaries; there is no new universal billing gateway. Legacy callers and deployment-level outbound network enforcement remain separately scoped.
- Runtime quotas, service latency, mutable alias identity and actual billing prices have not been measured. Do not present static values or a ping PASS as guarantees. No latency-based routing or invented quota controller was added.
- Verification is a preflight snapshot: a remote outage or credential change can still happen after validation. Synchronous per-attempt rechecks reduce stale local decisions; they cannot atomically lock an external provider's state.
- No migration, production secret change, provider activation, merge or deployment is authorized by these changes. Independent security acceptance and exact combined-tree integration evidence remain required.
