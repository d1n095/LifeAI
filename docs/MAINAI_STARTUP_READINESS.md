# MainAI Startup Readiness

Machine-checkable levels — **never one boolean**.

| Level | Meaning |
|---|---|
| `BLOCKED` | Core safety unhealthy |
| `READY_FOR_SAFE_INTERNAL_RUN` | Workforce + egress + authority present; provider gates may be unknown |
| `READY_FOR_LOW_RISK_PROVIDER_RUN` | All activation gates VERIFIED + Claude attestation + spend healthy |
| `READY_FOR_SERIOUS_AUTONOMOUS_RUN` | Prior + engine/memory/self-model/recovery healthy |

API: `app.mainai_startup_readiness.evaluate_startup_readiness(claude_reviews_satisfied=...)`.

`UNKNOWN != VERIFIED`. Provider activation uses `app.workforce.activation_gates.ActivationGateSet`.
