"""Turn suite logs into a verdict-ready summary: BLOCKER / REGRESSION / KNOWN_P2 / FIXED / OK per suite."""
import json, re, sys
from pathlib import Path

out, profile = Path(sys.argv[1]), sys.argv[2]
exp = json.loads((Path(__file__).parent / "expectations" / "expectations.json").read_text())
rows, verdict_blockers = [], 0

def spec(suite):
    s, seen = exp.get(suite, {}).get(profile), set()
    while s and "same_as" in s:  # resolve chains (combined -> codex -> cursor)
        if s["same_as"] in seen:
            raise SystemExit(f"expectations: same_as cycle in {suite}")
        seen.add(s["same_as"])
        s = exp[suite][s["same_as"]]
    return s

def pytest_results(text):
    res = {}
    for m in re.finditer(r"^(PASSED|FAILED|ERROR|XPASS|XFAIL) (\S+)", text, re.M):
        res[m.group(2)] = m.group(1)
    return res

def matrix_results(text):
    return {m.group(1): m.group(2) for m in re.finditer(r"^MATRIX\|([^|]+)\|(HOLDS|GAP)\|", text, re.M)}

def match(name, pats):
    return any(p == "*" or p in name for p in pats)

for log in sorted(out.glob("*.log")):
    suite = log.stem
    s = spec(suite)
    text = log.read_text(errors="replace")
    if s is None:
        continue
    status = {"suite": suite, "blocker": [], "regression": [], "known_p2": [], "fixed": [], "superseded": [], "passed": 0}
    if "required_matrix_hold" in s:
        for mid, res in matrix_results(text).items():
            if match(mid, exp.get("_informational_matrix", [])):
                continue
            if res == "HOLDS":
                status["passed"] += 1
                if match(mid, s.get("known_p2_matrix", [])):
                    status["fixed"].append(mid)
            elif match(mid, s.get("known_p2_matrix", [])):
                status["known_p2"].append(mid)
            else:
                status["blocker"].append(mid)
        for node, res in pytest_results(text).items():
            if res in ("FAILED", "ERROR"):
                status["regression"].append(node)
    elif "required" not in s:
        status["blocker"].append(f"SPEC_INVALID: no requirements resolved for {suite}/{profile}")
    elif "required" in s:
        res_all = pytest_results(text)
        if not res_all and re.search(r"ImportError|ModuleNotFoundError|AttributeError: module", text):
            status["blocker"].append("ADAPTER-NEEDED: suite could not import the candidate API")
        for node, res in res_all.items():
            ok = res in ("PASSED", "XFAIL")
            if ok:
                status["passed"] += 1
                if match(node, s.get("known_p2", [])):
                    status["fixed"].append(node)
            elif match(node, s.get("known_p2", [])):
                status["known_p2"].append(node)
            elif match(node, s.get("superseded", [])):
                status["superseded"].append(node)
            elif match(node, s["required"]):
                status["blocker"].append(node)
            else:
                status["regression"].append(node)
        if not res_all and not re.search(r"^ℹ pass \d+", text, re.M) and not status["blocker"]:
            status["blocker"].append("NO_RESULTS: required suite produced no parseable test results (crash, missing tool or empty run)")
        if re.search(r"^ℹ fail [1-9]", text, re.M):
            status["blocker"].append("node --test failures")
        if re.search(r"^ℹ pass (\d+)", text, re.M):
            status["passed"] += int(re.search(r"^ℹ pass (\d+)", text, re.M).group(1))
    rows.append(status)
    verdict_blockers += len(status["blocker"]) + len(status["regression"])

safety = out / "harness_safety.txt"
if safety.exists() and spec("candidate_harness"):
    s = spec("candidate_harness"); kv = dict(l.split("=", 1) for l in safety.read_text().splitlines() if "=" in l and not l.startswith("matrix"))
    st = {"suite": "candidate_harness", "blocker": [], "regression": [], "known_p2": [], "fixed": [], "passed": 0}
    for k, v in s["safety"].items():
        (st["passed"].__class__)
        if kv.get(k) != v:
            st["blocker"].append(f"{k}={kv.get(k)} (expected {v})")
    for line in safety.read_text().splitlines():
        if line.startswith("matrix_row"):
            _, res, tid = line.split(" ", 2)
            key = tid.split(" ")[0]
            (st["known_p2"] if key in s["allowed_matrix_fail"] else st["blocker"]).append(f"{res} {tid}")
    rows.append(st); verdict_blockers += len(st["blocker"])

ci = out / "ci_exact_sha.json"
if ci.exists():
    d = json.loads(ci.read_text()); st = {"suite": "ci_exact_sha", "blocker": [], "regression": [], "known_p2": [], "fixed": [], "passed": 0}
    if d.get("error"):
        st["blocker"].append("CI unreadable: " + d["error"])
    for name, results in d.get("required", {}).items():
        if results != ["success", "success"]:
            st["blocker"].append(f"{name}: {results}")
        else:
            st["passed"] += 1
    rows.append(st); verdict_blockers += len(st["blocker"])

summary = {"profile": profile, "blockers_and_regressions": verdict_blockers, "suites": rows}
(out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
for r in rows:
    print(f"{r['suite']:22s} pass={r['passed']:<4d} BLOCKER={len(r['blocker']):<3d} REGRESSION={len(r['regression']):<3d} KNOWN_P2={len(r['known_p2']):<3d} FIXED_P2={len(r['fixed']):<3d} SUPERSEDED={len(r.get('superseded', []))}")
    for b in (r["blocker"] + r["regression"])[:30]:
        print(f"    ! {b}")
print(f"\nEXECUTABLE GATE: {'PASS' if verdict_blockers == 0 else 'FAIL'} ({verdict_blockers} blocker/regression items). Founder/GitHub evidence (MATRIX.md, class F) is never covered by this gate.")
