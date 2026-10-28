# Personal Recall Security Boundary — `460e8ad8`

- **Subject:** the PostgreSQL authority boundary for Personal Recall on candidate head
  `460e8ad8cbd40b7fb0d62702f28c14412688e298`:
  - `personal_recall_owner_keys` — guard `personal_recall_key_guard` (migration 0095).
  - `personal_recall_grants` — guard `personal_recall_grant_guard` (migration 0095).
  - `personal_recall_sources` / `_chunks` / `_extractions` — protected personal data; FORCE
    owner-scoped RLS.
- **Suite:** `probes/fc_recallauth_v1` — **8 passed** (`RESULTS_460e8ad.txt`). DB-backed via the
  target's own `tests.conftest`; the authority substrate (owner key + grant) is built through
  the product's own privileged services bound to the canonical Founder identity
  (`FOUNDER_USER_ID`) + a live access JTI. Loopback-only egress guard; no paid calls; no
  production mutation; target worktree unchanged. `first_cycle_go=false`.
- **RECALL KEPT INACTIVE:** no ingestion, retrieval, snapshot or production activation was
  performed — only the minimal authority substrate needed to examine the DB guards. No changed
  identity is certified by relying on any prior certification; every property is proven on this
  exact SHA.

## Scope / non-duplication
The existing `tests/backend/personal_recall/` suite covers retrieval / indexing / snapshot
semantics; none of the six Recall DB-guard messages exercised here is asserted anywhere in
`tests/` (verified by grep). This is the unexamined DB authority surface.

## Examiner findings — all PASS

| ID | Property examined | Result |
|---|---|---|
| RC1 | Runtime **cannot mint a grant** — it lacks INSERT privilege (permission denied); and even if a future GRANT exposed INSERT, the guard refuses the runtime identity (`runtime cannot mint Personal Recall authority`) | PASS (dual layer) |
| RC2 | Runtime **cannot create or rotate an owner key** — INSERT privilege-denied; defense-in-depth guard refuses the runtime identity (`runtime cannot create or rotate Personal Recall owner keys`) | PASS (dual layer) |
| RC3 | **Fail closed**: a privileged caller setting `app.personal_recall_grant_authority='founder_authorized'` but WITHOUT current canonical founder authority (no live access JTI) cannot mint (`requires current canonical founder authority`) | PASS |
| RC4 | **Identity binding**: full canonical founder context cannot mint a grant for a *different* owner (`personal_recall_founder_authority_current(other)` is false → `requires current canonical founder authority`) | PASS |
| RC5 | A grant's bindings are **immutable; only one-way revocation** is permitted — the runtime may revoke (`revoked_at` NULL→set, exactly one row) but may **not** widen `can_retrieve`/`can_disclose` nor **un-revoke** (`bindings are immutable; only revocation is permitted`) | PASS |
| RC6 | Grant and owner key are **not deletable outside a governed erasure**, even though the runtime holds the DELETE privilege (`grants are revoked, not ordinarily deleted`; `owner keys are not ordinarily deletable`) | PASS |
| RC7 | **Protected-data isolation**: a foreign owner cannot see or mutate the grant/key (count 0; UPDATE matches 0 rows; grant stays live), and all of `sources`/`chunks`/`grants`/`owner_keys` have `relforcerowsecurity` with an isolation policy | PASS |
| RC8 | Runtime **cannot mutate an owner key at all** outside erasure — the key-material column is privilege-denied, and even on its granted `(status, revoked_at)` columns the guard refuses the runtime identity (`runtime cannot create or rotate Personal Recall owner keys`) | PASS |

### Interpreted result
On its exact SHA, the Personal Recall DB authority holds along every axis the task named:

- **Identity binding** — RC3/RC4: authority is bound to the canonical Founder owner + a live
  access JTI; a forged GUC or a cross-owner target fails closed.
- **Revocation** — RC5: one-way only; the runtime may revoke a grant but never widen or
  un-revoke it; RC8: the runtime cannot revoke/rotate a key directly at all (key authority is
  strictly founder-gated).
- **Erasure permissions** — RC6: deletion of grant/key is refused unless a governed erasure is
  current, despite the runtime holding the DELETE privilege.
- **Protected-data isolation** — RC7: FORCE owner-scoped RLS on the authority and content
  tables; a foreign owner sees/touches nothing.
- **Fail-closed** — RC1/RC2/RC3: minting requires the real canonical founder authority, not a
  privilege or a forged GUC; the runtime is doubly fenced (privilege + guard).

**No defect found in the Personal Recall database authority boundary on `460e8ad8`.** The
runtime is structurally unable to mint, rotate, widen, un-revoke, delete, or cross-read Recall
authority or keys; mutation requires the canonical Founder identity bound to a live session.
Recall was not activated, and no prior certification was relied upon.

## Scope / discipline
Read-only examiner; drove the real guards, privilege grants and RLS through the target's own
conftest; authority substrate built via the product's own services. Recall kept inactive (no
ingestion/retrieval/snapshot/activation). No paid calls; loopback only; no publication,
deployment or autonomy. Candidate materialised detached at the exact SHA and left unchanged.
`first_cycle_go=false`.

## Reproduce
```
probes/fc_recallauth_v1/run.sh ../../wtcert-460e8ad      # 8 passed
```

## Continuation status
Priorities 1 (Provider Spend), 2 (Transport) and 3 (Recall) of the continuous
spend/transport/Recall examination are complete on `460e8ad8`, each with an executable suite,
verdict and examiner commit, and no defect found. The effect-specific DB authority family
(envelope 0099, spend, transport, Recall) has now been independently examined end-to-end on
this lineage. Next distinct unexamined boundaries with concrete evidence available: the
account-erasure operation authority (migrations 0085/0086 — erasure-phase ordering, reauth
binding) and the founder capability/authority-change core (migration 0094).
