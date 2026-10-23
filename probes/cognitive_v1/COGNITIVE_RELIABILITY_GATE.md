# MainAI Cognitive Reliability Gate — CR-01..CR-20

- **Purpose:** make MainAI's operational reliability **measurable and enforceable** against the deterministic coordination substrate (event ledger, delivery gateway, dispatch gate, `mainai_cognitive_ops`), not against a model's conversational confidence.
- **Examiner-owned.** No production implementation or modification of frozen candidates. `first_cycle_go=false` unchanged. A working local bridge is never treated as a connected vendor agent.
- **Verified on:** frozen `#302 ff28b51` **and** candidate `#303 ec0f4d2` — **20 passed** on both (adapter-neutral; the gate exercises the provider-neutral substrate, so it is reusable across future GPT/Gemini/Claude/Cursor/Codex/local adapters).
- **Design rule (item 4):** an implemented+enforced invariant PASSES with an adversarial case + positive control; a non-deterministic capability is reported BLOCKED/FAIL, never a fabricated PASS. After examining `app/mainai_cognitive_ops/`, **zero CRs were left BLOCKED** — the substrate covers every requirement deterministically.

## Coverage matrix
| CR | Reliability requirement | Enforced by | Result | Evidence (print) |
|----|----|----|----|----|
| CR-01 | Out-of-order / early event resolves correctly, never applied as real state | `lifecycle.ingest_lifecycle_event` ordering + unknown-assignment guard | **PASS** | `early=stale_rejected good=applied` |
| CR-02 | Status never regresses on reload (monotonic chronology) | `durable` load-merge status-rank + obligation statuses | **PASS** | `statuses_after_reload=['completed']` |
| CR-03 | Duplicate report (same event_id) is idempotent | `lifecycle` processed_event_ids | **PASS** | `first=applied dup=replay_ignored` |
| CR-04 | Reported ≠ verified; dedup retains provenance; bundle reconstructable (interpretation substrate) | `mainai_cognitive_ops.situational_awareness` (PARTIAL≠COMPLETE), `information_lifecycle` (dedup provenance, defragment) | **PASS** | `all_complete=complete one_partial=partial dedup_prov=('src-A','src-B') reconstructable=True` |
| CR-05 | Event for unknown/foreign assignment rejected (ownership) | `lifecycle` unknown-assignment guard | **PASS** | `unknown_assignment=stale_rejected` |
| CR-06 | Cross-agent action (A acts on B's assignment) rejected | `lifecycle` `_actor_allowed` + identity map | **PASS** | `cross_agent=forged_rejected` |
| CR-07 | Agent ownership mapping is deterministic/stable | `lifecycle.identity_from_agent_key` | **PASS** | `mapping={claude,cursor,codex,examiner,cursor}` |
| CR-08 | No duplicate examiner obligation after repeated reconcile | `lifecycle.reconcile_owner` dedup | **PASS** | `examiner_count<=1` |
| CR-09 | Duplicate inbox prepare → one message | `delivery.prepare_message` idempotency_key | **PASS** | `first=prepared second=replay_ignored` |
| CR-10 | Claim replay not re-delivered | `delivery.claim_message` | **PASS** | `c1=delivered c2=replay_ignored` |
| CR-11 | Cross-agent claim rejected | `delivery.claim_message` recipient isolation | **PASS** | `cross_claim=forged_rejected` |
| CR-12 | No authorized transport → blocked, never silently delivered; no vendor claim | `delivery.attempt_transmit` | **PASS** | `blocked / local_authorized_bridge / claimed=False` |
| CR-13 | Deterministic pre-dispatch invariants fail closed (recipient capability, active/duplicate work, exact base SHA, authorized adapter, Founder approval) | `dispatch.evaluate_dispatch_readiness` + `service.evaluate_assignment_readiness` | **PASS** | `base=APPROVAL_REQUIRED stale_sha=STALE_BASE adapter=ADAPTER_DISABLED` |
| CR-14 | Self-reported completion never independently_verified | `delivery.submit_receipt` | **PASS** | `applied independently_verified=False` |
| CR-15 | Green GitHub CI ≠ independent certification | `lifecycle` github-ci receipt guard | **PASS** | `stale_rejected independently_verified=False` |
| CR-16 | Builder cannot self-verify | `lifecycle` completion vs verification | **PASS** | `iv=None` |
| CR-17 | execution_progress requires real evidence | `delivery.submit_receipt` | **PASS** | `empty_evidence=rejected` |
| CR-18 | Receipt carrying privilege/cert keys rejected (independently_verified/github_write/override_stop/stop) | `delivery.FORBIDDEN_RECEIPT_KEYS` | **PASS** | all four `forged_rejected` |
| CR-19 | No coordination/lifecycle event can clear STOP (positive control) | `lifecycle` actor/guard | **PASS** | `override_stop ignored → stale_rejected` |
| CR-20 | Unknown actor rejected; no vendor messaging channel claims usable | `lifecycle._actor_allowed` + `delivery_adapter_inventory` | **PASS** | `unknown_actor=forged_rejected vendor_usable=[False,False,False]` |

## Negative regression cases (embedded)
Every CR is adversarial-first: each asserts the **wrong** outcome is refused (forged/stale/replay/blocked/rejected) and, where applicable, that a **legitimate** control still succeeds (CR-01 good event applies, CR-10 first claim delivers, CR-13 base decision computes, CR-14 completion applies-without-certification). A regression that weakens any guard flips the corresponding CR to FAIL.

## What this gate deliberately does NOT claim
- It does **not** assert MainAI can reliably parse arbitrary free-text chat — raw NL interpretation is model-dependent and is **not** an authoritative state path. CR-04 instead locks the deterministic substrate MainAI must route conversation through (`mainai_cognitive_ops`): PARTIAL≠COMPLETE, SUMMARY≠SOURCE, DEDUPLICATED≠DELETED with provenance retained.
- It does **not** re-implement or re-test Codex's Security Kernel. CR-19 is a positive control that the *coordination layer* cannot clear STOP; the authenticated-reauth **closure** of the founder_ack gate (FA-9/CO-10) stays Codex-owned and is tracked BLOCKED in the release gate (`coord_accept_v1` CO-10), not duplicated here.
- It does **not** treat the available local bridge/CLI as a connected vendor agent (CR-12/CR-20; `vendor_usable=[False,False,False]`).

## Release blockers surfaced by this gate
**None new.** All 20 CRs pass on both SHAs. The reliability substrate is implemented and enforced. The standing release blockers remain the previously-recorded Codex-owned FA-9/CO-10 and FA-PRIV (see `audit_302/RELEASE_SECURITY_GATE.md`), which gate *unattended* autonomy, not this reliability gate.

## Reproduce
```
probes/cognitive_v1/run.sh ../wtcert-ff28b51   # 20 passed (frozen #302 base)
probes/cognitive_v1/run.sh ../wtcert-ec0f4d2   # 20 passed (candidate #303 — same substrate)
```
Integrate into the combined release gate as the reliability tier alongside `audit_302/gate.sh` Scope A.
