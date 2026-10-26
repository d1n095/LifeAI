# #309 corrective successor — PI-STOP, ICV-WIRING, PI-DEDUP, RST-4

Isolated child of examined `#309` @ `aff5de1a6e2a33266bf2fd00e2a3ce554daeafab`.
Does **not** amend `#309`, `#310`, or `#311`.

`first_cycle_go=false`. No live provider calls.

## Fixes

- **PI-STOP** — durable transitions call `assert_not_killed` and `assert_grant_allowed` on `workforce_authority_epoch`. No session or unreadable authority → refuse. STOP refuses completion across restart. Codex STOP-clear is untouched.
- **ICV-WIRING** — provider `FINISHED` is execution completion only. `continue_after_independent_verification` requires `find_independent_pass` on `mainai_verification_registry`. Builder self-certification cannot mint a PASS.
- **PI-DEDUP** — missing timestamps use a raw-body digest, not wall-clock, so replay still collapses.
- **RST-4** — bare and embedded `ghp_`, `sk-`, `key_live_`, `xoxb-`, and `Bearer` values are redacted, reusing development-operator patterns. Test fixtures assemble prefixes at runtime so GitHub push protection does not see scanner-shaped secrets.

Acceptance tests: `test_pistop_v1_*`, `test_indepcomp_v1_*`.
