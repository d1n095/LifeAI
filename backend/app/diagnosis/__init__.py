"""Deterministic recording/query API for Life's own causal diagnosis interface -- observation,
hypothesis, and proven-cause kept as genuinely distinct epistemic stages, a failed step never
automatically implying a code regression. See `service.py`'s own module docstring for the full
doctrine."""

from app.diagnosis.service import (
    DiagnosisError,
    get_diagnosis,
    list_current_diagnoses,
    list_diagnoses,
    list_unresolved_diagnoses,
    prove_diagnosis_cause,
    record_diagnosis,
    rule_out_diagnosis,
)

__all__ = [
    "DiagnosisError",
    "get_diagnosis",
    "list_current_diagnoses",
    "list_diagnoses",
    "list_unresolved_diagnoses",
    "prove_diagnosis_cause",
    "record_diagnosis",
    "rule_out_diagnosis",
]
