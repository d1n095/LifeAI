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

## Explicitly deferred

- **Ingesting the founder's real corpus.** This harness exists so that trial can eventually
  happen safely and be scored -- it is not that trial. The bundled `fixtures.CORPUS` is
  synthetic, small, and deliberately adversarial, never real founder material.
- **A persisted trial-run record / history of past trial scores.** `run_trial()` returns an
  in-memory `TrialReport`; nothing about a trial run is written to the database. Once a real
  trial is closer, a durable record of trial runs (reusing the same authority/basis/provenance
  discipline as every other foundation here) is a natural, small follow-up -- not built now.
  A UI surface showing trial history is out of scope for the same reason.
- **`app.problem_learning` (life_problems/life_problem_decisions, migration 0042) is not
  wired into the bundled fixtures.** It already has its own `active_decision()` current-state
  query and the same supersession discipline, so it needs no new plumbing -- but its object
  graph (problem -> approach/component -> decision) is heavier than `founder_memory`/
  `diagnosis`'s flat records, and folding it into this bootstrap increment's fixtures was not
  necessary to prove the seven dimensions. Extending the harness's fixture set to exercise it
  is a natural, narrow future addition, not a redesign.
- **Automatic contradiction DETECTION** (noticing on its own that two recorded facts
  conflict) -- still deliberately never built, consistent with every other foundation in this
  mission; the harness only scores whether an EXPLICIT contradiction (a caller calling
  `mark_founder_memory_disputed`/`rule_out_diagnosis`) is handled correctly, never whether the
  system notices contradictions itself.
