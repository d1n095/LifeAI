# Founder Alpha local-AI evidence — 2026-11-22

**Code SHA:** `4f99518010cdf46df764cf6a9e5a5b6539095a06`
**Tree SHA:** `46ea9421d87706269fe605eaec6aefa036e7768f`
**Branch:** `freebuff/life-workspaces`

Environment: isolated PostgreSQL 16 + pgvector on `127.0.0.1:55432`, Redis 7 on
`127.0.0.1:56379`, real FastAPI backend on `127.0.0.1:8010`, real Next.js frontend on
`127.0.0.1:3020`, real durable worker, and Chromium through Playwright. No production
resource was used.

## Result summary

- Focused backend suite: **57 passed**, 0 failed, 0 skipped; JUnit time 104.41 s.
- Browser journeys: **13 passed**, 0 failed (V6 2, personal work 1, model routing 2,
  resilience 8).
- TypeScript: PASS. ESLint: 0 errors, 1 pre-existing warning in `frontend/lib/api.ts`.
- Production build: PASS, 29 routes.
- Short soak (2026-11-22): **F23 BLOCKED** (45 s < 14,400 s), 0 request failures, 24 chats,
  24 streams, 4 grounded retrieval chats, 4 document imports, 8 MainAI jobs, 16 concurrent
  health checks, 1 backend restart.
- Four-hour soak (2026-11-23): **PASS**, `f23_status=PASS`, 14,400 s, 0 failures, 0 blockers —
  see `soak-4h.json`.
- Real Ollama model: **PASS on 2026-11-23** — Ollama 0.40.15 with `qwen2.5:0.5b` and
  `all-minilm`; see `summary-2026-11-23.json` and the addendum in
  `docs/FOUNDER_ALPHA_LOCAL_AI.md`.

The resilience suite initially reported two failures against stale test assumptions, not
product defects: `/api/documents` is no longer called by the dashboard, and a project name
matched both a link and a select option. The assertions were changed to `/api/dashboard`
and the project link; the rerun passed 8/8. The failed run is not presented as a pass.

## Artifacts

| File | Contents |
| --- | --- |
| `backend-tests.txt` / `.xml` | Full focused backend run and machine-readable JUnit |
| `frontend-tsc.txt` | `tsc --noEmit` |
| `frontend-lint.txt` | ESLint result |
| `frontend-build.txt` | Next.js production build, route table |
| `playwright.txt` | V6, personal work and model-routing journeys |
| `playwright-resilience.txt` | Cancel, network retry, idempotency, reload, keyboard, project journeys |
| `soak-short.json` / `.txt` | Timestamps, latency percentiles, resources, counts and BLOCKED verdict |
| `soak-4h.json` | Four-hour F23 run: 464 samples, counts, latency percentiles, final DB/Redis state and the harness's own PASS verdict |
| `summary.json` | Compact machine-readable certification summary |

## Interpretation limits

The soak's chat latency is application overhead under the deterministic fake provider used by
the E2E backend, not real local-model generation latency. The 45-second run is explicitly not
endurance evidence; the four-hour `soak-4h.json` is. The absence of Ollama means the live local-model discovery path is
unit/API tested and its UI is browser tested with a mocked discovery response, but no installed
Ollama model was selected or invoked here.

Reproduction commands are in `docs/FOUNDER_ALPHA_LOCAL_AI.md`. Delivery verification is in
`delivery/founder-alpha-local-ai/VERIFY.txt`.
