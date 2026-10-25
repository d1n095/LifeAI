# HD-1 — Synthetic Ledger Origin Trust Boundary (threat model + evidence)

- **Examined SHA:** #306 `2444bcc3da8ffa5ca10ef3295bac13aa045769c6` (current best candidate; not a release-blocker analysis).
- **Question:** after the CF-2 fix, synthetic-ledger identity is decided SOLELY by the mutable provenance boolean `created_without_manual_assignment` — via `persist._is_backing` and `lifecycle.reconcile_owner`'s event-skip. Is that single field a safe origin-of-truth under future transports, migrations, import/recovery, and untrusted provenance?
- **Examiner:** Claude, read-only; executable negatives on the real persistence/gateway boundary; no production change; no duplication of Codex DB-privilege or Cursor agent-side work. `first_cycle_go=false`; vendor connectivity and unattended autonomy remain separately BLOCKED.
- **Suite:** `hd1_v1` — **8 passed**.

## Evidence matrix
| # | Scenario | Result | Reachability |
|---|---|---|---|
| HD1-1 | Forged `created_without_manual_assignment` on a legitimate work assignment | `_is_backing=True` on a real builder (agent_key=cursor-*, supports_write=True) → it would be treated as synthetic ledger storage and its events skipped by reconcile | **HYPOTHETICAL** — requires a direct provenance write (the Codex-owned FA-PRIV surface); no app path sets it (HD1-7) |
| HD1-2 | Synthetic-origin marker tampering during import/recovery | No import/recovery path restores `AgentWorkAssignment.provenance` (RAG zip/library import + storage act on Document provenance; recovery_takeover does not write assignment provenance) | **NOT A VECTOR** |
| HD1-3 | Inconsistent agent identity vs synthetic metadata | A codex-agent row carrying the marker still classifies synthetic; `_is_backing` never cross-checks `agent_key == mainai-ledger` | **HYPOTHETICAL** (same write prerequisite as HD1-1) |
| HD1-4 | Synthetic marking with unexpected WRITE capability | A `read_write` / `supports_write=True` row carrying the marker classifies synthetic; genuine synthetic is `read_only`/`supports_write=False` — not cross-checked | **HYPOTHETICAL** |
| HD1-5 | Multiple/conflicting synthetic backing rows | Two synthetic rows → `ensure_owner_ledger_backing` picks one **deterministically and stably** (same pick on repeat), no crash, `persist_status` coherent | **BENIGN** (handled) |
| HD1-6 | Corrupted/incomplete provenance after restart | A partial `durable_coordination` dump on a real row: fresh-session `repair` + `load_owner_ledger` do not raise; the real row does NOT classify synthetic (`backing_rows=0`) | **REACHABLE, BENIGN** |
| HD1-7 | Unauthorized modification via DB/API paths | Driving the real receipt/gateway flow — including a receipt that tries to inject `created_without_manual_assignment` — sets the marker on **0** assignments. The sole writer is `_create_backing_assignment`. | **NOT REACHABLE via the application layer** |
| HD1-8 | Historical provenance requiring repair | `repair_misclassified_backing` demotes the contaminated real row (`_is_backing=False`) and preserves the genuine synthetic row (`_is_backing=True`) | **CORRECT** |

## Threat-model findings
1. **No currently-reachable application attack path (HD1-2, HD1-7).** Nothing an agent, a receipt, an HTTP caller, an importer, or the recovery path can do sets `created_without_manual_assignment` on a work assignment. The classification is safe against untrusted *input* today.
2. **The trust boundary is the database row itself.** The classification trusts one mutable provenance boolean with **no corroboration** by canonical agent identity, write restriction, or a tamper-resistant origin record (HD1-1/3/4). Any actor able to write that provenance field can:
   - make a **real** assignment be treated as synthetic → its lifecycle events are silently skipped by `reconcile_owner`, and `repair_misclassified_backing` will **not** recover it (repair early-returns for `created_without_manual` rows). Silent sidelining of real work.
   - (the inverse, CF-2, is fixed: a stray `ledger_backing` marker no longer matters.)
3. **The only actor with that write today is the `mainai_app` runtime role** via the **Codex-owned FA-PRIV** surface (it can UPDATE `agent_work_assignments`). So **HD-1 compounds FA-PRIV; it is not an independent application-layer vulnerability.** It also becomes newly relevant if a *future* agent transport, DB migration, or import/recovery feature ever writes assignment provenance from less-trusted data — which is exactly why the corroboration below should land before those features.

## Severity
**P3 — defense-in-depth / hardening.** Not reachable through any application path on #306 (HD1-7); reachable only by an actor who already holds FA-PRIV-level DB write (a separate, already-tracked Codex blocker). **Not an independent new release blocker for Scope A**, and no release blocker is manufactured here. It should be closed as part of hardening before (a) unattended autonomy and (b) any future provenance-restoring transport/migration/import path.

## Clear separation (reachable now vs hypothetical future)
- **Reachable now via application input:** none.
- **Reachable now via DB write:** yes, but only through FA-PRIV (Codex-owned) — compounds that finding.
- **Hypothetical future:** a new agent transport / migration / import-recovery path that writes assignment provenance from untrusted or externally-supplied data would make HD-1 directly reachable. The corroboration contract pre-empts this.

## Minimal implementation contract (for the confirmed gap; Cursor/MainAI — not Codex DB work)
Make synthetic classification corroborated and tamper-resistant, so a single provenance boolean cannot sideline real work:
1. **`_is_backing(row)` must require ALL of** (not just the boolean):
   - `provenance.created_without_manual_assignment is True`, AND
   - canonical identity: the row's agent `agent_key == LEDGER_AGENT_KEY` (`"mainai-ledger"`), AND
   - write restriction: that agent's `supports_write is False` **and** `read_write_mode == read_only`.
   A row failing any of these is treated as a REAL assignment (never skipped by reconcile), even if the boolean is set.
2. **Trusted origin record (stronger):** record synthetic origin in a dedicated, append-only/immutable store (or derive it from `requested_by == "coordination"` + the canonical agent), rather than a row-mutable provenance field, so FA-PRIV-level provenance edits cannot flip classification.
3. **Holder ≠ synthetic:** keep the ledger *dump* on a real `rows[0]` if desired, but never let the presence of that dump imply synthetic origin (already true post-CF-2); classification must depend only on origin corroboration (1/2).
4. **Regression:** `hd1_v1` HD1-1/3/4 flip from "classifies synthetic" to "classifies REAL" once corroboration lands (invert those asserts to lock the fix); HD1-7/HD1-2 stay as the no-reachable-path guard; HD1-8 stays green.

## Reproduce
```
probes/hd1_v1/run.sh ../wtcert-2444bcc   # HD1-1..8: 8 passed
# writer audit: grep -rn "created_without_manual_assignment" backend/app  -> sole writer _create_backing_assignment
```
HD-1 does not delay #306's completed Scope-A examination; it is a forward hardening item tied to FA-PRIV and future provenance-writing features.
