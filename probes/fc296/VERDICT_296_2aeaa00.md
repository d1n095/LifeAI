# Independent examination — #296 (Security Diagnostics & Threat Monitoring)

- **Candidate:** PR #296 (draft), branch `cursor/security-diagnostics-91c1`.
- **Examined SHA:** `2aeaa00efe4ffe4c5264b725d28e2e97b3bfa7f0`. Five commits on #295 `97e2b04`; ancestry verified. Exact-SHA CI: all 12 required checks + aggregate green in push and pull_request.
- **Production delta:** 20 files, +2112/−3. New package `app/mainai_security/` (analysis, monitoring, incidents, leak, remediation, restrictions, dashboard), router `/api/mainai/security/*`, `/admin/security` page, founder-ops snapshot hook.
- **Examiner:** Claude, read-only. Hermetic git; provider recorders + egress guard (zero paid calls, zero non-loopback); no merge/deploy/Recall.

## Verdict: RETURN_TO_BUILDER — #296 itself is clean; inherited R1 (P1) still blocks release

**The #296 module introduces no new defect I could find, and its security design is sound.** The release is nonetheless RETURN_TO_BUILDER because the inherited #295 P1 (R1, global Founder lockout) is still open and must not disappear from the release verdict (assignment item 9).

### #296 module — new code, verified (security_diag_v1, 10/10 pass + controls)

| Check | Result |
|---|---|
| SD-1 authorization | Every `/api/mainai/security/*` endpoint (dashboard, restrictions, remediation propose/approve/apply, incidents preserve/acknowledge) refuses anon, an ordinary member, and a role-only `founder` impostor → 401/403. |
| SD-2 / SD-2b no autonomous remediation | `apply` is **always refused**, via the endpoint **and** a direct `refuse_apply` service call; a sensitive payload (`github_write_enabled`, `secret_key`) → 409. Settings are unchanged; `applied=False`, `live_checkout_written=False`. |
| SD-3 forged audit events | 50 forged unknown-action rows → **0** incidents (`monitoring` ignores non-`KNOWN_ACTIONS`). |
| SD-3b fabricated known rows | Graded by count: a few rows stay `uncertain`; no incident is `confirmed` on thin evidence; no recommendation instructs an autonomous action ("Do not disable accounts automatically"). |
| SD-4 / SD-4b secret redaction | 10 secret shapes (OpenAI, AWS, GH PAT/fine-grained, Slack, JWT, PEM, postgres URL, bearer, `secret_key=…`) are detected and masked; the raw value never appears in the redacted text or a finding. Positive control: ordinary text is preserved and not false-positived. |
| SD-5 / SD-5b truthful status | The security dashboard carries `confidence` and `evidence` on findings, and `first_cycle_go=False`; it explicitly states it is **not** a GO input. The founder-ops snapshot stays `first_cycle_go=False`. |
| SD-6 incident preservation | A preserved incident with a raw secret and `{"grant_founder": true, "role": "founder"}` is redacted (no raw secret echoed) and mutates no settings. |
| Candidate's own suite | `test_mainai_security_diagnostics.py`: **16 passed** in one uninterrupted run. |

Analysis evidence is grounded in real config/filesystem/lockfile facts (`analysis.py`), not asserted. Remediation is proposal→approval(intent-only)→**apply-refused**. This is a careful, fail-closed design.

### Item 7 — does the monitor trust mutable `audit_log`? (LEGACY-rooted)

**The module does not treat forged rows as authoritative** (SD-3/3b). But its evidence source, `audit_log`, **remains runtime-writable** by `mainai_app` on #296 (FAC-5: `UPDATE, DELETE` held, no RLS — the exact `security_kernel_v1` FA-PRIV finding, still open). So a model-driven path could still (a) insert many known-action rows to fabricate a `confirmed` **alert** (noise, not privilege gain), or (b) **delete** real rows to hide an incident. This is an **evidence-integrity limitation inherited from the legacy mutable-audit finding**, not a new #296 defect; the monitor layers honest confidence grading and no-autonomous-action on top of it. **Severity P2, LEGACY (Codex-owned DB hardening).** Correct fix is the Security Kernel P1 remediation (append-only `audit_log`); the monitor should additionally prefer tamper-evident sources where available.

### Inherited defects (item 9) — must remain on the ledger

Correction gate on `2aeaa00` (frontend built): **R1, D4, D5, N1, N4 open; R2, R3, R4, R5, R6, D1, D2, P-1, N2/N3 pass** — i.e. #296 preserves every #295 fix and adds no regression. 5 gates open, identical to #295.

| ID | Sev | Status on #296 |
|---|---|---|
| **R1** | **P1** | Open (inherited from #295). Proxy forwards `x-forwarded-for: unknown` for every client → shared rate-limit identity → global Founder lockout. **Release blocker.** |
| D4 | P2 | Open (inherited): stale "Backend: nåbar" after backend loss. |
| D5 | P3 | Open (inherited): readiness BLOCKED shown with zero blockers. |
| N1 | P2 | Open (inherited): invented "Nej" disagreement on a Founder question. |
| N4 | P2 | Open (inherited): "acknowledged" from a historical dispatch event. |
| LEGACY FA-PRIV | P1 | Open (Codex-owned): `users`, `audit_log`, STOP, envelope, spend runtime-writable. |

### First-cycle safeguards (item 8)

`first_cycle_go` stays `False` on both the security dashboard and the founder-ops snapshot; the module declares itself not a GO input. Exact-SHA CI green. The two `migration_roundtrip` failures observed in an ad-hoc run are an examiner harness artifact (alembic resolved against the probes dir, not `backend/`) — they fail identically on #295 and are **not** a #296 regression.

### Correction requirements

- **#296 is accept-worthy on its own merits** once the module's evidence-integrity note is acknowledged: when the Security Kernel P1 lands (append-only `audit_log`), the monitor's evidence becomes trustworthy; until then the dashboard should mark audit-derived findings as resting on a mutable source.
- **Release remains blocked by R1 (P1)** and the legacy FA-PRIV P1 — both owned outside this PR.

## Reproduce

```
probes/security_diag_v1/run.sh ../wtcert-2aeaa00                 # SD-1..SD-6 + controls
probes/fa_core_v1/run.sh ../wtcert-2aeaa00 test_fac_core.py -k FAC5   # item 7 legacy audit_log mutability
probes/cursor_corrections_v1/run.sh ../wtcert-2aeaa00 <built-frontend>  # inherited R1/D4/D5/N1/N4
```
