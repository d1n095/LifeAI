# Activation-security reference probes

Run locally without services, secrets or network:

```bash
python3 docs/evidence/activation_security/activation_security_probe.py
```

The script is a pure reference oracle for the adversarial cases in
`docs/MAINAI_ACTIVATION_SECURITY_IMPLEMENTATION_SPEC.md`. It does not test production code and
must not be reported as implementation verification. During implementation, each vector must
be translated into tests against the real database services, Founder API and GitHub adapter.

Required production variants additionally cover:

- two concurrent database transactions consuming the same grant (exactly one succeeds);
- crash after consumption/before push and remote reconciliation;
- PR base/head/head-SHA drift;
- same check name from the wrong GitHub App;
- envelope/spend/ruleset/token revocation between preflight and every boundary;
- legacy ready and retryable tasks present while the activation goal runs;
- inherited npm-audit failure parks without creating a provider reservation.
