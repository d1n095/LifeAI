# Cursor correction gate (after #294 `27e945d` RETURN_TO_BUILDER)

`run.sh <candidate-worktree> <built-frontend-dir>` runs **only** the gates below and prints one verdict line per defect.

Every gate has a **positive control**, so a candidate cannot pass by disabling the feature. Every gate exercises production code paths:
- the real HTTP app;
- the real worker cycle;
- the real `next start` proxy;
- a real backend started with the Dockerfile uvicorn flags;
- a real Chromium.

| Defect | Sev | Requirement | Gate (all must PASS) | Control |
|---|---|---|---|---|
| R1 | **P1** | Spoofed forwarding headers never set the rate-limit identity, **and** distinct real clients keep distinct identities. | PX-9 (rotating spoofed XFF cannot bypass), PX-10 (client B unaffected after A exhausts its limit), PX-11 (A forging B's address cannot exhaust B) | PX-9a, PX-10a (A itself is limited) |
| P-1 | P2 | The proxy never leaves `/api/`. | PX-1 | PX-3..PX-6 (proxy still forwards) |
| R2 | P2 | An independent receipt is bound to the task's own commit SHA; invalidated or superseded PASSes do not count. | AR-18a, AR-18b, AR-18c | AR-18d (a matching PASS **does** verify) |
| D1 | P1 (closed) | Builder self-verification is never "independently verified". | AR-11, AR-11b | AR-18d |
| D2 | P1 (closed) | One paid call per concurrent dispatch. | AR-9, AR-9h | — |
| R3 | P2 | A failed paid dispatch is re-dispatchable and is not shown as running. | AR-9f | AR-9 |
| R4 | P2 | A Founder cancel survives a worker crash (`cancelled`, no takeover, no paid call). | FC (stop_classification G) | — |
| R5 | P2 | An ambiguous publication-boundary failure leaves an observation/recovery record; never replay. | FO-6b/c (stop_classification F) | FO-6x, RL-1..3 (P1 closed in #294; must stay green) |
| R6 | P3 | Safe-mode verification runs on the tree that contains the proposal; no false PASS. | AR-13c | AR-13, AR-13b |
| D4 | P2 | No positive health or STOP claim survives a failed poll or a lost backend. | UI-1, UI-2 (Playwright) | each test first asserts the healthy state |
| D5 | P3 | Readiness BLOCKED is counted among blockers. | AR-17, UI-3 | — |
| N1 | P2 | No "Nej …" disagreement when the Founder merely asks. | AR-14a | AR-14b (a real claim is still contradicted) |
| N4 | P2 | `acknowledged` requires a live claim or lease, not a historical event. | AR-15 | — |
| N2, N3 | closed | Recall-specific text; live evidence. | AR-14b, AR-14d | — |

Codex-owned gates are run by the `combined` profile (`../certify_v1/`), not by this package:
- grants (GC-1..GC-8, #280 matrix);
- credential-free transport (TC-1/TC-3, CRED);
- publication history (TA-12, TA-13, PUB-2);
- migration integrity.
