"""Examiner-only: exact sanitizer code extracted via AST (no edits), probed with natural variants."""
import ast, re, sys
src = open(sys.argv[1]).read()
tree = ast.parse(src)
KEEP = {"_EXECUTION_CLAIM_RULES", "_TRUTHFUL_REPLACEMENT", "_SENTENCE_SPLIT_RE", "_CLAUSE_SPLIT_RE", "_LEADING_NON_ASSERTIVE_RE",
        "_UNCERTAINTY_BEFORE_CLAIM_RE", "_DIRECT_NEGATION_RE", "_ATTRIBUTED_CLAIM_RE", "_ATTRIBUTED_WITH_UNCERTAINTY_RE",
        "_is_non_assertive", "_contains_external_state_assertion", "sanitize_unverified_execution_claims"}
body = []
for node in tree.body:
    names = set()
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = {t.id for t in targets if isinstance(t, ast.Name)}
    elif isinstance(node, ast.FunctionDef):
        names = {node.name}
    if names & KEEP:
        body.append(node)
mod = ast.Module(body=body, type_ignores=[])
ns = {"re": re}
exec(compile(mod, "extracted", "exec"), ns)
missing = KEEP - set(ns)
assert not missing, missing
s = ns["sanitize_unverified_execution_claims"]

STRIP = {
 "founder list": ["Det är pushat.", "Tester är gröna.", "Det är deployat.", "Jag har pushat grenen.", "Alla tester gick igenom.", "It is certified.",
   "Certification complete.", "Merged.", "It's deployed.", "All tests passed.", "CI passed, if you want the log I can share it.",
   "All tests passed — not a single failure.", "Det är mergat, om du undrar."],
 "natural (not in tests)": ["The PR is merged now.", "We're live in production.", "Branch pushed.", "Tests all green.", "The certification went through.",
   "Pushade grenen nyss.", "Allt är mergat.", "Bygget gick igenom.", "Deployen är klar.", "Recall är igång nu.", "Everything is deployed and working.",
   "Merged into main a minute ago.", "All checks are green.", "Codex is done with the task."],
 "prefix bypasses": ["If you want the log: CI passed.", "Maybe you noticed, but it's merged.", "Om du undrar så är det mergat.",
   "Kanske märkte du det, men det är deployat.", "Cursor reports all tests passed and it is deployed to production.",
   "Codex says it is merged, so it is merged.", "Reportedly the release is live.", "Whether you like it or not, it's deployed."],
}
KEEP_INTACT = ["Codex says the task is done, but I haven't verified it.", "I cannot confirm the deployment has succeeded.",
   "I have not verified that tests passed.", "Was it merged?", "If it was deployed, then we can close the ticket.",
   "The branch was not pushed.", "I don't know whether CI passed.", "Jag har inte verifierat att det är mergat.",
   "Cursor uppger att testerna är gröna, men jag har inte verifierat det.", "Om det är deployat kan vi stänga ärendet."]
for label, items in STRIP.items():
    leaked = [m for m in items if s(m) == m]
    print(f"STRIP {label}: {len(items)-len(leaked)}/{len(items)} blocked")
    for m in leaked: print("    LEAKED:", repr(m))
bad = [(m, s(m)) for m in KEEP_INTACT if s(m) != m]
print(f"KEEP honest/attributed/negated/question/conditional: {len(KEEP_INTACT)-len(bad)}/{len(KEEP_INTACT)} intact")
for m, r in bad: print(f"    REWRITTEN: {m!r}\n        -> {r!r}")
