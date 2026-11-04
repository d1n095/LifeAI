# Security Diagnostics & Threat Monitoring

Reusable Life OS security module. MainAI can discover weaknesses, monitor audit
signals, investigate leaks and prepare remediation proposals. The Founder keeps
approval authority over sensitive changes. This is not a separate AI system.

## Ownership

- Cursor owns diagnostics, monitoring and the Founder-facing operational surface.
- Codex owns Founder Authority & Capability Management Core and secure transport.
- Claude owns independent security examination.
- Frozen `#295` is not amended by this module.

## What it does

1. **Analysis** — settings, manifests, leaked-secret patterns, suspicious local diffs, world-writable files, auth weaknesses.
2. **Threat monitoring** — existing `audit_log` rows only. Confirmed vs uncertain.
3. **Leak prevention** — detect and mask secrets. Raw values are never copied into findings, logs or API responses.
4. **Founder dashboard** — `/admin/security` and `GET /api/mainai/security/dashboard`.
5. **Explainable restrictions** — cite the actual setting/wiring source. Unknown capabilities return `source_unknown` and never invent policy.
6. **Remediation** — research and propose. Installation, permission, encryption, authentication, network and policy changes require Founder approval and are still not applied by this module.
7. **Incidents** — preserve redacted evidence, notify via the dashboard, recommend recovery/rollback. No privilege escalation.
8. **Adversarial tests** — false positives, forged audit actions, prompt injection, policy tampering, cross-account leakage, malicious dependencies, authorization bypass.

## What it does not do

- Does not change first-cycle GO. `blocks_first_cycle` is always false.
- Does not install packages, mutate security policy or write the live checkout.
- Does not duplicate Codex transport/grants or Claude examination.
- Does not run live npm audit or paid provider calls.
- Does not add Alembic tables. Durable records reuse `audit_log`.

## HTTP

Founder-only (`require_founder`):

- `GET /api/mainai/security/dashboard`
- `GET /api/mainai/security/restrictions`
- `POST /api/mainai/security/remediation/propose`
- `POST /api/mainai/security/remediation/{id}/approve` — records intent, `applied=false`
- `POST /api/mainai/security/remediation/{id}/apply` — always `409`
- `POST /api/mainai/security/incidents/preserve`
- `POST /api/mainai/security/incidents/{id}/acknowledge`

A compact summary is also attached to the existing Founder-ops snapshot as
`security`, without becoming a blocker.
