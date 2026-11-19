# Founder Console + Living Core

Route `/` in the existing frontend shell. No second application was created: the console composes
the routes and APIs that already existed (`/chat`, `/library`, `/workbench`, `/admin/jobs`,
`/admin/mainai-execution`), and the sidebar groups them by the founder's job.

## What the console shows, and where every value comes from

| Surface | Source (real event) | Never done |
|---|---|---|
| Model status | `GET /api/health` → `local_model.reachable`, `missing_models` | A green dot without a probe; `missing_models` absent means **unknown**, not "none missing" |
| Worker | `health.worker` (`alive` / `unknown`) | Treating "API up" as "jobs run" |
| Storage | `health.storage.writable` (real write probe) | A capacity/health claim from config |
| Database | `health.status`, which itself checks `SELECT 1` | Static "operational" copy |
| Running work | `GET /api/mainai/jobs`, polled every 10 s while visible | A progress bar from a client-side timer |
| Cancel | `POST /api/mainai/jobs/{id}/cancel`, only for statuses the API accepts | Showing a cancel button the backend would reject |
| Approval required | execution goals → task detail, `approval_required && !completed` | Mixing approval-gated tasks into "autonomous" work |
| Decisions | `GET /api/library?classification=decisions`, `active_truth_status=active` | Inferring a decision from a chat message |
| Conversation | `POST /api/chat` through the same-origin proxy | A canned answer or a fake streaming bubble |
| Degraded state | Any failed dependency renders an explicit banner; partial fetches are named individually | Blanking the page or hiding the failure |

The console has no placeholder telemetry, no invented security score and no "all systems
operational" sentence. `Kräver ditt godkännande` lists only tasks the backend marks
`approval_required`; everything shown as ordinary activity is either a durable job or a navigation
link.

## Living Core

`frontend/components/living-core/LivingCore.tsx` is a single canvas component. It imports no API
client, auth, job, approval or security module — it cannot decide, authorize or observe anything.
Callers pass a state they derived from real events:

| State | Set by |
|---|---|
| `resting` | nothing active |
| `reasoning` | a chat request is in flight |
| `creating` | at least one queued/running/paused durable job |
| `listening` / `speaking` | the browser's speech recognition/synthesis callbacks |
| `degraded` | health fetch failed, API unavailable, model unreachable, missing model, or a chat error |
| `security` | available for a security-area view; it is a visual mode, not a claim that anything is secure |

Modes (`harmony`, `care`, `creativity`, `curiosity`, `focus`, `protection`) are presentation
contexts, not inferred emotions. The console labels them "Visuellt läge" and states in the UI that
the Core is not a feeling and not a security assessment.

Rendering and cost:

- layered radial gradients on one canvas, five layers normally, three in low-power mode;
- eased palette/motion transitions over ~1.2 s plus a decaying trail (afterglow), no abrupt reset;
- devicePixelRatio capped at 2, frame cap 30 fps (20 low-power), rendering paused while
  `document.hidden`;
- `prefers-reduced-motion: reduce` draws one static frame and never starts the animation loop;
- low power = `hardwareConcurrency <= 4 || deviceMemory <= 4`, or reduced motion;
- `ResizeObserver` for sizing, all listeners removed on unmount;
- the canvas is `aria-hidden`; the factual state is announced by the surrounding `role="status"`.

## Verification

- `frontend/e2e/founder-console.spec.ts` — 3/3 in Chromium: console + status with no backend error,
  a real local-model answer with sources from the console composer, and reduced-motion fallback
  (`data-motion="reduced"`).
- `frontend/e2e/founder-alpha-workflow.spec.ts` — 5/5, unchanged workflow still passes.
- Screenshot: `docs/evidence/founder-console.png` (captured by the test itself, not hand-made).
- TypeScript and `next build` pass.

## Known limits

- Tested in desktop Chromium only; no mobile viewport pass yet.
- Voice is browser-dependent (`SpeechRecognition`/`speechSynthesis`); no audio capture is stored or
  uploaded, and unsupported browsers simply do not render the mic control.
- Job/approval state is polled, not pushed; a change can take up to 10 s to appear.
- Cancellation and approval are covered by real backend API tests
  (`test_mainai_execution_api.py`, `test_mainai_execution_demos.py`) and the console calls those
  endpoints; the browser test does not click a live cancel button because no long-running job is
  guaranteed to exist at test time.
- The Living Core is decorative. It must never be used as evidence that a job, model or security
  control is healthy; the status rows are.
