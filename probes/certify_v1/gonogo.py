"""Truthful GO/NO-GO matrix from a `combined` acceptance summary. A row is GO only if every mapped suite ran and has
zero blockers and regressions. Class-F (Founder/GitHub) rows are always NOT ATTESTED -> NO-GO unless attestation evidence
is supplied separately. Overall GO requires every row GO. Usage: gonogo.py summary.json ci_exact_sha.json sha"""
import json, sys
summ = json.load(open(sys.argv[1])); sha = sys.argv[3]
suites = {r["suite"]: r for r in summ["suites"]}
AREAS = [
    ("Founder authority, one-use grants, replay prevention, typed denial", ["authorization_grants", "grant_contract"]),
    ("Credential isolation / credential-free transport", ["credential_canary", "transport_contract", "candidate_harness"]),
    ("External failures terminal, no paid re-run (incl. FO-6), STOP/recovery", ["stop_classification", "review_stop"]),
    ("Publication content and history (TA-12/13, PUB-2), task authority", ["task_authority"]),
    ("Spend controls", ["spend"]),
    ("Migration integrity (single head, no dual 0089)", ["migration_integrity"]),
    ("Application readiness (authz matrix, truthfulness D1-D5, races, safe launch)", ["app_readiness"]),
    ("Same-origin proxy (traversal, XFF trust, rate limits)", ["proxy"]),
    ("UI truthfulness in a real browser", ["ui"]),
    ("Candidate regression + FC-4 policy", ["backend_regression", "fc4_policy", "fc4_bypass"]),
    ("Exact-SHA CI (12 checks x push+PR)", ["ci_exact_sha"]),
]
FOUNDER = ["Integration branch at approved base SHA", "Ruleset: PR required, 12 checks bound to app 15368, no bypass, Founder-only merge",
           "Runtime GitHub App identity + permission manifest, no classic PAT", "Signed attestation material + pinned verification key",
           "First-cycle spend grant + envelope profile", "Reviewer identity + receipt runbook", "Incident token revocation + evidence publication"]
print(f"# GO/NO-GO — `{sha}`\n\n| Area | Suites | Result | Detail |\n|---|---|---|---|")
all_go = True
for name, keys in AREAS:
    missing = [k for k in keys if k not in suites]
    bad = sum(len(suites[k]["blocker"]) + len(suites[k]["regression"]) for k in keys if k in suites)
    go = not missing and bad == 0
    all_go &= go
    detail = (f"missing: {missing}; " if missing else "") + (f"{bad} blocker/regression" if bad else "clean")
    print(f"| {name} | {', '.join(keys)} | {'GO' if go else '**NO-GO**'} | {detail} |")
for f in FOUNDER:
    print(f"| {f} | class F | **NO-GO (not attested)** | Founder/GitHub evidence required |")
print(f"\n**Overall: {'GO' if all_go and not FOUNDER else 'NO-GO'}** (executable rows {'all GO' if all_go else 'not all GO'}; class-F rows require Founder attestation).")
