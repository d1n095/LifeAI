# LifeAI project-knowledge inventory — Pass 2

Founder directive, 2026-11-15 ("STOP IDLING, CONTINUE ENGINEERING"), Priority 2: Pass 1 (PR
#430, 158 records) "is reconnaissance, not a complete inventory." This pass addresses, with
measured evidence rather than assertion, each item the founder named: false positives/
negatives, deduplication and stable identity, source versioning/supersession, expanded
coverage, mapping to modules/tests/status, preserved provenance/uncertainty, and a
reproducible coverage report. **Nothing here is ingested into any authoritative table, and
nothing here creates an executable task** — same governance as Pass 1.

## 1. False positive / false negative assessment (measured, not asserted)

**Method:** a `random.seed(42)` stratified sample of 25 of Pass 1's 158 records, each
manually read in its source file for context and classified as a genuine, atomic,
traceable requirement/decision/dependency statement (true positive) or not (false
positive/borderline). Full sample and per-record judgment available on request; headline
result below.

**Finding: precision is NOT uniform across classifiers.** Of the 25 sampled records, 20
happened to be `superseded_marker` and 5 were from the other 7 classifiers combined
(roughly proportional to their 57.6%/42.4% population split). The 5 non-`superseded_marker`
samples (one each of `build_block_marker`, `checkbox_done`, and three
`status_declaration_marker`) were **5 of 5 genuine true positives** — e.g.
`docs/SECURITY_BLOCKERS.md:9`'s `**Status: åtgärdat.**` line is a real, dated, verifiable
security-fix record (the exact `next@14.2.15` CVE remediation `CLAUDE.md` itself already
cites as precedent).

The 20 sampled `superseded_marker` records (91 of 158 total — the single largest category)
split as **6 clear true positives, 6 borderline (real but too generic/context-dependent to
stand alone), and 8 clear false positives** — a strict precision of 6/20 (30%), or 12/20
(60%) if borderline cases are counted as usable. The false positives are heavily
vocabulary-shaped: this corpus uses "superseded"/"supersedes"/"ersätter" often to describe
**enum values or relationship-type lists**, not a specific supersession **event**. Examples
of real false positives found in the sample: `docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md:35`
("`active_truth_status` (active/historical/proposed/superseded/disputed...)") and
`docs/CURSOR_ADVERSARIAL_RUNTIME_LANE_HANDOFF.md:116` ("- contradictory / stale / superseded
/ duplicate evidence") — both just enumerate a vocabulary, not a decision.

**I tested, and rejected, a stricter auto-filter** (require a `#PR-number` or an ISO date
alongside the keyword) before writing any code: it correctly dropped 80 of 91 records, but
it ALSO dropped `docs/AUTH_THREAT_MODEL.md:1` — `"# Hotmodell och designbeslut —
cookie-baserad session (ersätter JWT i localStorage)"` — this repo's own top-level session-
auth architecture decision, which cites no PR number or date inline. **Trading recall for
precision here loses real signal, not just noise.** The correct fix is NOT more regex
engineering — it is the `confidence` tier added in §2, so a human reviewer can triage low-
confidence records faster, consistent with this project's own "signal producer != truth
writer" governance (already required before ANY promotion, regardless of confidence).

**False negatives, with a concrete example:** `docs/SECURITY_BLOCKERS.md` lines 15-30
contain substantive, real decisions and verification steps — "Uppgraderat kontrollerat till
`next@16.2.10`...", "**Verifierat innan commit:** `npm audit` (0 sårbarheter), `tsc
--noEmit` (rent)..." — that Pass 1's 8 marker-based patterns completely miss, because they
use neither a checkbox, "Beslutat", "superseded", nor a `**Status:**` prefix. This confirms
Pass 1's own §4 ("Pass 2+ candidates: full semantic claim extraction via the real ingestion
pipeline") rather than contradicting it: expanding marker-pattern coverage further has
rapidly diminishing returns (a bracket-tagged-heading pattern tried for this file matched
only 2 lines across the entire 111-file corpus — not worth a dedicated classifier).

## 2. Deduplication and stable requirement identity

`backend/scripts/mainai/extract_project_knowledge_inventory_pass2.py` adds a `record_id`
per record: a SHA-256 hash of `(source_file, proposed_entity_type, text)`, truncated to 16
hex chars — **deliberately not based on `line_no`**, so the same requirement keeps the same
identity across re-runs even if unrelated edits earlier in the file shift its line number.
Measured on the real 158-record Pass-1 output: **158 unique IDs, 0 collisions** — no two
distinct records currently hash to the same identity, and no single record appears twice.

## 3. Source versioning, supersession, contradictory decisions

- **Versioning:** every record now carries `source_last_commit` — that file's real
  `git log -1 --format=%H` hash at extraction time (one `git log` call per unique file,
  cached). A future re-run can diff this against the current hash to know exactly which
  source files changed since last extraction, without re-scanning everything from scratch.
- **Supersession references:** a `references` field extracts any `#<number>` or
  `YYYY-MM-DD` found in the record's own text (e.g. `docs/CLAUDE_AUTO_RECOVERY_AUTHORITY_LANE.md:3`'s
  `"**SUPERSEDED.** #165 has merged..."` yields `["#165"]`). Measured: **10 of 158 records**
  carry an extractable reference — a real, if small, directed edge a human reviewer can
  follow, not a fabricated graph.
- **Contradictory decisions:** not separately computed beyond §1's finding that
  `superseded_marker` already surfaces candidate contradiction/supersession language; cross-
  referencing those 91 candidates against each other's actual targets (which document
  supersedes which) needs the real `ProjectEntity.supersedes_entity_id` promotion path and
  human judgment, not a script — correctly out of scope for reconnaissance.

## 4. Expanded source coverage

Re-ran Pass 1's exact 8 patterns against `docs/BRANCH_REGISTRY.md` (excluded from Pass 1's
stable baseline specifically because it is a continuously-changing, session-authored log —
see Pass 1's own §2): **34 additional candidate records across 8,829 lines** (15
`superseded_marker`, 13 `status_declaration_marker`, 3 `conflict_marker`, 2
`open_question_marker`, 1 `build_block_marker`). Reported here as a separate, labeled
measurement — **not merged into the stable 158-record Pass-1/Pass-2 baseline**, for the same
stability reason Pass 1 excluded it to begin with.

**Still not expanded, named explicitly:**
- **PR bodies / commit history** (hundreds of PRs) — a real, large, separate corpus; the
  directive names this but also says "do not start... unrelated implementations" — scanning
  it would need `app.github_evidence`'s already-existing repo-wide read capability (per Pass
  1's own §4), itself a nontrivial follow-up task, not attempted here.
- **Source code itself** (docstrings, inline comments) — not scanned; a different corpus
  shape (code, not prose) that would need different patterns entirely.
- **External founder materials** (anything outside this repository) — no access path exists
  from this session to any such material; cannot be assessed, named as a real unknown rather
  than silently ignored.
- **Access boundaries for future private documents** — this pass only ever read repository
  files already in this session's normal read scope. No mechanism here reads anything a
  future private/restricted document store would need separate authorization for; this
  extraction approach does not itself define what that authorization should look like.

## 5. Mapping atomic requirements to existing modules/tests/implementation status

For the 52 `requirement` records (the `checkbox_pending`/`checkbox_done` category), a
deliberately coarse, keyword-based heuristic (`candidate_module` field) suggests a plausible
backend module by simple substring match (e.g. "calendar" → `app/calendar`) — **labeled
exactly for what it is: a weak hint for a human to verify, never a claim of correctness.**
Measured: **10 of 52 (19.2%)** matched any keyword at all. This low number is itself the
honest finding — most checklist items in this corpus (mostly from `docs/ROADMAP.md` and
plan documents) describe features in prose that doesn't share vocabulary with the actual
module names, so a real mapping would need either a maintained alias table or, again, the
real semantic pipeline — not attempted further here to avoid a second round of regex
engineering with the same diminishing-returns problem §1 already demonstrated.

No attempt was made to map requirements to specific TESTS or to automatically classify
"implementation status" beyond what Pass 1 already inferred from checkbox state
(`implemented` for `- [x]`, `not_started` for `- [ ]`) — doing this honestly needs either the
module mapping above to be far more reliable than 19.2%, or real human triage.

## 6. Provenance and uncertainty — preserved, not weakened

Every Pass-1 field (`source_file`, `line_no`, `classifier_strategy`, `text`) is carried
through unchanged in `docs/LIFE_PROJECT_KNOWLEDGE_INVENTORY_PASS2.json`. New fields
(`record_id`, `confidence`, `source_last_commit`, `references`, `candidate_module`,
`promoted`) are strictly additive. `promoted` is hardcoded `false` for every record this
script writes — a reviewer or founder decision, never this script, is what would ever flip
it, and nothing reads that field as authorization for anything.

## 7. Reproducible coverage report

| Metric | Value |
|---|---|
| Pass-1 records carried through | 158 |
| Unique stable `record_id`s | 158 (0 collisions) |
| High-confidence records | 67 (42.4%) |
| Low-confidence records (`superseded_marker`) | 91 (57.6%) |
| Records with an extracted `#PR`/date reference | 10 (6.3%) |
| `requirement` records with a candidate module hint | 10 / 52 (19.2%) |
| Extended-corpus candidates in `BRANCH_REGISTRY.md` (separate, not merged) | 34 / 8,829 lines |
| Sampled manual precision, non-`superseded_marker` classifiers | 5/5 (100%, n=5 of the 25-record sample) |
| Sampled manual precision, `superseded_marker` | 6/20 strict, 12/20 incl. borderline (30–60%, n=20 of the 25-record sample) |

Re-run via `python3 backend/scripts/mainai/extract_project_knowledge_inventory_pass1.py`
then `python3 backend/scripts/mainai/extract_project_knowledge_inventory_pass2.py`, in that
order, any time the corpus changes.

## What this pass explicitly does not do

- Does not ingest any record into `KnowledgeClaim`/`InterpretationProposal`/`ProjectEntity`/
  `WorkCandidate` — every record's `promoted` field is hardcoded `false`.
- Does not create any executable task, goal, or work candidate from any extracted record.
- Does not claim the `superseded_marker` precision problem is fixed — it is measured,
  explained, and deliberately left for human review rather than papered over with a
  recall-losing filter.
- Does not claim complete source coverage — §4 names exactly what remains unscanned and why.
- Does not replace `docs/LIFE_PROJECT_KNOWLEDGE_INVENTORY_MODEL.md`'s architecture mapping
  (Pass 1) — this pass only adds measurement and enrichment on top of it.
