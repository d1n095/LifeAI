# Pre-examination — #293 (Founder workflow readiness) at current head `ee132bb8e87fcc99f2f3607cfc2b46a5364d23c7`

**NOT A VERDICT.** The PR states that the head is not frozen until exact-SHA CI is green. The verdict will be issued on the frozen SHA only.

#293 was built on #292 `5e3cd01` before the #292 verdict was issued.

## Carried from #292 (`app_readiness_v1` on ee132bb)

AR-9 and AR-9h (paid dispatch race, D2), AR-10 (delegation race, D3), and AR-11 and AR-11b (self-verification shown as independent, D1) still fail. AR-1..AR-8, AR-12 and AR-13 hold.

## New in #293 (`test_ar14_founder_workflow.py`)

| ID | Sev | Finding | Exact output / evidence |
|---|---|---|---|
| N1 | P2 | **Invented disagreement.** A Founder *question* that mentions GO gets a "Nej … evidensen motsäger påståendet" prefix. `_GO_CLAIM_RE` matches the phrase `first-cycle GO` anywhere in the message, so a claim and a question are treated alike. This is persisted into every matching chat reply. | `Vad blockerar first-cycle GO?` → `Nej. First-cycle GO är false. Evidensen motsäger påståendet att systemet är redo att gå. First-cycle GO blockeras av …` (AR-14a) |
| N2 | P3 | A Recall claim is contradicted with the GitHub-write text: `_RECALL_CLAIM_RE` appends `_WRITE_DISAGREEMENT_SV`. | `Recall är på nu.` → `Nej. GitHub-skrivning och privilegierade fjärreffekter är avstängda …` (AR-14b) |
| N3 | P2 | The chat route passes **hard-coded** evidence (`independently_verified: False`, `recall_http_mounted: False`) to the disagreement policy. A true Founder statement that work is independently verified will always be answered "Nej". | `routers/chat.py` (AR-14d) |
| N4 | P2 | **"Acknowledged" masks re-queued or stranded work.** Any historical `dispatched` event makes a `ready` task with no live job show as `acknowledged` ("accepted, not executing"). This includes the FO-6 stranded-`ready` task after its paid re-runs. | AR-15 (status `ready`, job `failed`, old `dispatched` event → `acknowledged`) |
| — | design | A repeated identical answer is replaced by "Jag har inget nytt att tillägga." and persisted. This is per the stated policy (item 4), so it is only noted. | direct call |
| OK | — | Cycles: every member of a 3-cycle refuses assignment, and the cycle is surfaced as a blocker and as `break_circular_dependency`. A known cost is not rewritten as unknown. | AR-16, AR-14c pass |
