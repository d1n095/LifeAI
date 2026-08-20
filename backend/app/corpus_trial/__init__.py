"""Life Corpus Trial Harness -- the minimal evaluation harness foundation for a later, real,
controlled mixed-corpus trial. See docs/LIFE_CORPUS_TRIAL_HARNESS.md for the full scope and
`harness.py`'s own module docstring for what it actually runs."""

from app.corpus_trial.fixtures import CORPUS, CorpusItem
from app.corpus_trial.harness import TrialReport, run_trial
from app.corpus_trial.persistence import CorpusTrialRunError, list_trial_runs, record_trial_run
from app.corpus_trial.scoring import SCORERS, RecordSnapshot

__all__ = [
    "CORPUS",
    "CorpusItem",
    "CorpusTrialRunError",
    "RecordSnapshot",
    "SCORERS",
    "TrialReport",
    "list_trial_runs",
    "record_trial_run",
    "run_trial",
]
