# LIFE CORPUS TRIAL HARNESS

## Foundation scope

This document defines the foundation introduced by `app/corpus_trial/` (no new migration):
the minimal evaluation harness for a LATER, real, controlled mixed-corpus trial. It answers
mission item 7 of "LIFE SELF-MODEL, ADAPTIVE COGNITION & CORPUS READINESS." It does NOT
ingest the founder's actual corpus, and it does NOT build a persisted "trial run" record --
both are explicitly deferred (see below). What it builds is a way to answer, on demand and
against a small deliberately mixed test corpus, "if Life recorded these facts through her own
real provenance systems right now, would source preservation, attribution, epistemic
distinction, contradiction handling, supersession, uncertainty, and current-state
reconstruction all survive intact?"

## Why no new schema

Every provenance capability the harness scores already exists and was built in prior
increments of this same mission: `app.founder_memory` (migration 0049) and `app.diagnosis`
(migration 0050), both themselves reusing migration 0042's `authority`/`basis` vocabulary and
following the same "never rewrite `content`/`observation` in place, supersede via a new row"
discipline. The harness's job is to prove that discipline holds end-to-end when a genuinely
mixed set of facts flows through the real recording APIs together -- not to introduce a new
place to store facts. Building a competing "corpus trial record" table before the systems it
would score even had a chance to be exercised together would have been exactly the kind of
premature, parallel structure the mission's own item 8 warns against.

## One genuine small extension: `list_current_diagnoses()`

Researching what "current-state reconstruction" would need surfaced one real, narrow gap:
`founder_memory_notes` and `life_problem_decisions` both auto-flip an old row's own `status`
to `superseded` the moment a new row supersedes it (so "what do we currently believe" is
already a plain `status="active"` filter, and both already had one). `diagnosis_records`
deliberately does NOT do this -- superseding a diagnosis is a fact about the new row, never a
mutation of the old one (see migration 0050's own docstring), so there was no existing query
answering "the latest diagnosis in each supersession lineage." `app.diagnosis.service.
list_current_diagnoses()` fills exactly that gap: a diagnosis is "current" when no other row
for the same owner names it as `supersedes_diagnosis_id`. `ruled_out`/`proven_cause` rows stay
included unless something has superseded them -- by design, reaching a resolved state about
an observation is not the same as that observation being replaced by a different one; the
diagnosis reaching resolution is still the current, up-to-date understanding of that lineage.

## A third system: `app.problem_learning`

Deliberately left out of the bootstrap corpus at first (see PR #102's own "Explicitly
deferred" note, now resolved). `app.problem_learning` (migration 0042, predates this mission)
already has its own `record_decision()` auto-supersession (the SAME "contradiction excludes
currency" semantics `founder_memory_notes` uses, not `diagnosis_records`' different one) and
its own `active_decision()` current-state query -- no new plumbing was needed in `scoring.py`
to exercise it, only two new fixture items and a third `system` branch in `harness.py`. Its
object graph is heavier (`LifeProblem` -> `LifeProblemDecision`, versus `founder_memory`/
`diagnosis`'s flat records), and it has no in-place "mark contradicted without a replacement"
transition the way `mark_founder_memory_disputed()`/`rule_out_diagnosis()` do -- every status
change there is a brand-new superseding row, so the bundled fixtures exercise recording and
supersession for this system, not the `contradiction_detection` dimension specifically (the
other two systems already prove that dimension is a real, working check).

## Principles

- **Real code paths, not mocks.** `harness.py` replays the corpus through
  `app.founder_memory.record_founder_memory`/`mark_founder_memory_disputed` and
  `app.diagnosis.record_diagnosis`/`prove_diagnosis_cause`/`rule_out_diagnosis` -- the exact
  functions production code calls. A trial that scored a synthetic mock of these systems would
  prove nothing about the real ones.
- **Structural scorers, not fixture-tuned expectations.** Every function in `scoring.py`
  checks a general invariant ("the old row's text must never change when superseded," "a
  `None` confidence must never read back as a number") computed from the read-back snapshot
  graph itself, never a hardcoded list of expected IDs or content strings. The same scorer
  would catch the same class of violation on a completely different corpus. `tests/backend/
  mainai/test_corpus_trial_harness.py` proves this directly: every scorer has its own
  standalone test that hand-builds a deliberately corrupted snapshot and confirms the scorer
  catches it, independent of whether the bundled fixtures happen to trigger that path.
- **The bundled corpus is deliberately adversarial, not friendly.** `fixtures.py`'s corpus
  includes a statement that *reads* like a confident fact ("The founder is definitely always
  working from UTC+1") but is recorded with `authority="unknown"` -- the harness must catch a
  system that "upgrades" confident-sounding text to certain provenance, not just one that
  mishandles obviously-uncertain text.
- **One trial, one owner, fully isolated.** `run_trial()` takes an explicit `owner_id` and
  never touches another owner's rows -- the same RLS/ownership boundary every other foundation
  in this codebase already enforces structurally, not just by convention here.

## The seven scored dimensions

`source_preservation`, `attribution_accuracy`, `epistemic_distinction` (decision vs.
suggestion vs. inferred pattern vs. unknown provenance never collapse into one bucket),
`contradiction_detection`, `supersession_detection`, `uncertainty_preservation`,
`current_state_reconstruction`. `TrialReport.passed` is true only when all seven have zero
violations; `TrialReport.summary` gives a per-dimension PASS/FAIL(count) view.

## Existing systems reused

`app.founder_memory`, `app.diagnosis`, `app.intelligence_governance` (`record_execution`/
`record_evidence`, to ground the corpus's own `prove_diagnosis_cause` step in a real evidence
row -- the harness fabricates no evidence of its own).

## Trial run history (migration 0052)

`run_trial()` itself stays pure -- no DB write beyond the corpus recording it already requires
to run. `app.corpus_trial.record_trial_run()` is a separate, explicit, opt-in step that
persists a durable snapshot of one `TrialReport` to `corpus_trial_runs`: `corpus_label`,
`record_count`, `passed`, `dimension_summary` (the PASS/FAIL(count) view), `violation_counts`.
Never a copy of the corpus or the `founder_memory_notes`/`diagnosis_records` rows the trial
exercised -- those remain independently queryable in their own tables. Idempotent by
construction, same discipline as every other recording function in this mission.

Deliberately NOT shaped like `capability_records`/`founder_memory_notes`/`diagnosis_records`:
a trial run is not a provenance CLAIM about the world (no founder statement, no inference), so
it has no `authority`/`basis` columns and does not reuse migration 0042's vocabulary. It IS an
append-only execution/evidence record structurally, so it reuses THAT pattern instead --
`capability_observation_events`'s DB-trigger-enforced append-only guarantee (a direct
UPDATE/DELETE is rejected even for a superuser session, not just hidden by RLS), not
`founder_memory_notes`/`diagnosis_records`'s mutable-with-narrowed-privileges pattern. A
corrected or re-run trial is always a NEW row.

`app.corpus_trial.list_trial_runs()` -- the read path, optionally filtered by `corpus_label`
(so a bootstrap-fixtures run and a later, differently-labeled corpus's runs stay distinguishable
in the same table without needing a separate one per corpus version).

## Explicitly deferred

- **Ingesting the founder's real corpus.** This harness exists so that trial can eventually
  happen safely and be scored -- it is not that trial. The bundled `fixtures.CORPUS` is
  synthetic, small, and deliberately adversarial, never real founder material.
- **A UI surface showing trial history.** Data + service layer only, matching every other
  "foundation" layer in this codebase.
- **Automatic contradiction DETECTION** (noticing on its own that two recorded facts
  conflict) -- still deliberately never built, consistent with every other foundation in this
  mission; the harness only scores whether an EXPLICIT contradiction (a caller calling
  `mark_founder_memory_disputed`/`rule_out_diagnosis`) is handled correctly, never whether the
  system notices contradictions itself.
