# Independent-completion acceptance — the eventual real provider cycle

**Property:** a completion is certified only when a NON-builder examiner records an independent
PASS, bound to one exact candidate SHA, with evidence; the record cannot be forged by the examined
runtime; and the provider cycle must consult that record before treating a completion as verified.

**Candidate:** #309 `aff5de1a`. **Suite:** `probes/indepcomp_v1` — **10 passed, 1 xfailed(strict),
1 skipped(BLOCKED)**. Read-only, network-free, no live/paid calls. `first_cycle_go=false`.

Drives the real `app.mainai_verification_registry.service` + model guard (migration 0080) +
`app.agent_coordination.provider_ingestion`.

## What holds today (PASS)
| ID | Property | Mechanism |
|---|---|---|
| A1 | builder self-certification rejected (examiner==builder) | `_require_independent` |
| A2 | a bare status is not evidence (empty `evidence_summary` rejected) | validation |
| A3 | verification bound to one exact 40-hex candidate SHA | `_require_sha` |
| A4 | `review_result` ∈ {PASS,FAIL,BLOCKED} | validation |
| **B1** | **ordinary MainAI runtime (`session_user=mainai_app`) CANNOT insert a verification record** | DB trigger `trg_mainai_verification_records_guard` (SECURITY DEFINER) |
| **B2** | records are **append-only** (UPDATE/DELETE blocked even for the privileged writer) | same trigger |
| C1 | `find_independent_pass` returns a PASS only from a non-builder examiner | registry query |
| C2 | an invalidated pass (`invalidates_verification_id`) is excluded | registry query |
| C3 | owner-scoping: a different owner's / a global query does not match an owner row | registry query |
| D1 | a real provider FINISHED ingestion creates **no** independent pass (`independently_verified=False`) | provider_ingestion + registry |

B1/B2 are a strong independence control: the examined runtime cannot manufacture its own
independent-verification pass, and history cannot be rewritten. Only a privileged (non-runtime)
examiner writer can attest.

## Required integration not yet done (xfail strict — fails now, XPASS→red if fixed)
- **ICV-WIRING (E1):** the provider cycle (`app.agent_coordination.*`) does not import or call
  `find_independent_pass` before treating a provider completion as verified / eligible for
  continuation. The registry exists and is consulted by `mainai_execution/review_receipt.py` and
  `mainai_founder_boot/readiness.py`, but NOT by the provider-event path. Required: on a provider
  FINISHED with no independent pass recorded, continuation must be gated
  (`requires_independent_verification`) until a non-builder examiner PASS for that exact candidate
  SHA exists. Parallels PI-STOP: both are pre-round-trip wiring of an existing, sound subsystem
  into the provider ingestion/continuation path.

## Blocked on a real authenticated round trip (skip)
- **ICV-REAL (F1):** independent verification of REAL authenticated provider evidence (non-fixture)
  recorded as a PASS that gates the real cycle — needs a Founder-authorized live Cursor Cloud key,
  a real round trip, and the PI-STOP + ICV-WIRING wiring. No fixture may ever be recorded as an
  independent/vendor PASS.

## Acceptance when implemented
On a composed, authorized round trip: ICV-WIRING flips to PASS (the provider cycle refuses to treat
a completion as verified without an independent, non-builder, exact-SHA PASS in the registry), and
ICV-REAL becomes runnable (a genuine independent examiner attests over real evidence, within STOP /
spend authority). Until then, independent completion is enforceable at the registry level but NOT
wired into the provider cycle, so the real cycle stays NOT CERTIFIED / Gate B NOT PERFORMED.

## Reproduce
```
probes/indepcomp_v1/run.sh ../../wtcert-aff5de1   # 10 passed, 1 skipped, 1 xfailed
```
