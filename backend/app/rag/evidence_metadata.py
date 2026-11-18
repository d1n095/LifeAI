"""The evidence-quality METADATA PRODUCER — the missing half of grade_evidence()'s edge.

`app/rag/evidence_quality.grade_evidence()` is a pure rubric over a structured metadata
dict. Until this module existed it had NO production caller: nothing produced the dict, so
every live `classify_claim()` ran uncapped and the whole evidence-quality layer was
standalone (tested, but connected to nothing). This module is the producer the Round 6
report named as the next step for that layer:

  founder / import pipeline DECLARES structured study facts for a claim
    -> normalize_declared_metadata()   (fail-closed: unknown keys rejected with a reason,
                                        never invented, never guessed)
    -> grade_evidence()                (the existing pure rubric, run ONCE, at stamp time)
    -> claim_evidence_metadata row     (declaration + MATERIALIZED verdict: grade, ladder
                                        cap, caveats, per-axis findings)
    -> read paths consume it:
        * app/routers/library.py detail view: classify_claim(quality_cap=...) per claim,
          with grade/cap/caveats surfaced beside the verdict
        * app/rag/epistemics.epistemic_status_counts_sql(): LEFT JOIN + three-way
          substitution so the chat/workbench instruction rollup agrees with the detail view

Why materialize at write time: the rollup is deliberately single-SQL (per-request call
sites must not run N per-claim verdicts), and re-implementing the rubric in SQL would be a
second implementation of one rule — a future disagreement waiting to happen. The cap lives
in the row, the SQL only substitutes over the grader's own closed vocabulary
({verified_fact, hypothesis, unknown}).

Bias rules (the mission's own, enforced structurally, inherited from evidence_quality):
neither mainstream nor alternative sources are automatically correct — the rubric grades
the DECLARED structure (design, duration, dose, interactions, conflicts, competing
explanations, causal support), not the source's popularity or nativeness. A funding
conflict limits the grade and is always surfaced as a caveat, and is never automatic proof
of invalidity; a speculative mechanism caps at hypothesis; correlation-only caps at
hypothesis. Absence of metadata is honest unknown — never a guess dressed as a grade.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.claim_evidence_metadata import ClaimEvidenceMetadata
from app.models.knowledge_claim import KnowledgeClaim
from app.rag.evidence_quality import EvidenceQualityAssessment, QualityGrade, StudyDesign, grade_evidence
from app.rag.epistemics import EpistemicStatus

DECLARED_VIA_MAX = 60
# M4 hardening: declared values are CONTENT (free text describing a study), but structure
# is bounded — a 100MB "dose" string or a 10^50 sample size is an attack, not a study.
# The key whitelist already bounds the key count; these bound each VALUE.
VALUE_MAX_CHARS = 4096
SAMPLE_SIZE_MAX = 10**9


@dataclass
class NormalizedMetadata:
    """What a raw declaration became: the accepted dict (exactly what the grader saw),
    and per-key rejection reasons for everything that was NOT accepted. `rejected` is the
    audit trail — a founder who typos `study_designs` gets told, rather than silently
    graded as if the field were absent."""

    declared: dict = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)


def _as_str_list(value) -> list[str] | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    return None


def _str_key(key: str):
    def _norm(value, rejected: dict[str, str]):
        if not isinstance(value, str):
            rejected[key] = f"expected a string, got {type(value).__name__}"
            return None
        if len(value) > VALUE_MAX_CHARS:
            rejected[key] = f"value exceeds the {VALUE_MAX_CHARS}-character per-field limit"
            return None
        text = value.strip()
        if not text:
            rejected[key] = "empty string is treated as absent, not recorded"
            return None
        return text

    return _norm


def _bool_key(key: str):
    def _norm(value, rejected: dict[str, str]):
        if not isinstance(value, bool):
            rejected[key] = f"expected a boolean, got {type(value).__name__}"
            return None
        return value

    return _norm


def _str_list_key(key: str):
    def _norm(value, rejected: dict[str, str]):
        if isinstance(value, list) and len(value) > 64:
            rejected[key] = "list exceeds the 64-item per-field limit"
            return None
        items = _as_str_list(value)
        if items is None:
            rejected[key] = "expected a string or a list of strings"
            return None
        if any(len(v) > VALUE_MAX_CHARS for v in items):
            rejected[key] = f"an item exceeds the {VALUE_MAX_CHARS}-character per-field limit"
            return None
        cleaned = [v.strip() for v in items if v.strip()]
        if not cleaned:
            rejected[key] = "empty value is treated as absent, not recorded"
            return None
        return cleaned

    return _norm


def _study_design_key(key: str):
    def _norm(value, rejected: dict[str, str]):
        if not isinstance(value, str):
            rejected[key] = f"expected a string, got {type(value).__name__}"
            return None
        if len(value) > 256:
            rejected[key] = "value exceeds the 256-character limit for a vocabulary field"
            return None
        try:
            return StudyDesign(value.strip()).value
        except ValueError:
            valid = ", ".join(d.value for d in StudyDesign)
            rejected[key] = f"'{value}' is not a known study design; valid: {valid}"
            return None

    return _norm


def _vocabulary_key(key: str, valid_values: tuple[str, ...], label: str):
    """Closed-vocabulary string keys (statistical_reporting, replication): an out-of-
    vocabulary value is REJECTED with the valid set named — fail-closed at the producer,
    so the grader only ever sees values from its own vocabulary (it still defends itself
    for direct callers, but the stamp path never relies on that)."""
    def _norm(value, rejected: dict[str, str]):
        if not isinstance(value, str):
            rejected[key] = f"expected a string, got {type(value).__name__}"
            return None
        if len(value) > 256:
            rejected[key] = "value exceeds the 256-character limit for a vocabulary field"
            return None
        stripped = value.strip()
        if stripped not in valid_values:
            rejected[key] = f"'{value}' is not a known {label}; valid: {', '.join(valid_values)}"
            return None
        return stripped

    return _norm


def _int_key(key: str):
    def _norm(value, rejected: dict[str, str]):
        if isinstance(value, bool):
            rejected[key] = "expected an integer, got a boolean"
            return None
        if isinstance(value, int):
            if value < 0:
                rejected[key] = "sample size cannot be negative"
                return None
            if value > SAMPLE_SIZE_MAX:
                rejected[key] = "sample size exceeds any plausible study size (cap 10^9)"
                return None
            return value
        rejected[key] = f"expected an integer, got {type(value).__name__}"
        return None

    return _norm


# The exact vocabulary grade_evidence() reads — a closed whitelist. Keys outside it are
# rejected, not silently stored: storing a key the grader never reads would be metadata
# theater (the founder believes they declared something that changes the verdict; it
# doesn't).
DECLARED_KEYS: dict[str, object] = {
    "study_design": _study_design_key("study_design"),
    "exposure_duration": _str_key("exposure_duration"),
    "dose": _str_key("dose"),
    "dose_response_documented": _bool_key("dose_response_documented"),
    "population": _str_key("population"),
    "outcomes": _str_list_key("outcomes"),
    "interactions": _str_list_key("interactions"),
    "conflicts_of_interest": _str_key("conflicts_of_interest"),
    "competing_explanations": _str_list_key("competing_explanations"),
    "causal_claim": _bool_key("causal_claim"),
    "causal_claim_supported_by_design": _bool_key("causal_claim_supported_by_design"),
    # Round 8 M2: methodology depth + replication + short/long-term + descriptive setting.
    # study_setting is deliberately graded NOWHERE — it feeds the evidence-landscape
    # summarizer's diversity report, and grading by geography/institution would be exactly
    # the "mainstream vs alternative" bias the mission forbids.
    "sample_size": _int_key("sample_size"),
    "statistical_reporting": _vocabulary_key(
        "statistical_reporting", ("confidence_intervals", "p_values", "bayesian", "none_reported"),
        "statistical reporting value",
    ),
    "replication": _vocabulary_key("replication", ("independent", "same_team", "none"), "replication value"),
    "outcome_followup": _str_key("outcome_followup"),
    "study_setting": _str_key("study_setting"),
}


def normalize_declared_metadata(raw: dict | None) -> NormalizedMetadata:
    """Fail-closed normalization of a raw declaration into grader vocabulary.

    Every accepted key passes through; every rejected key comes back with a named reason.
    Nothing is ever inferred, defaulted or invented here — the grader's honest-unknown
    contract depends on this side staying honest too."""
    result = NormalizedMetadata()
    if not isinstance(raw, dict):
        result.rejected["__root__"] = "metadata must be a JSON object"
        return result
    for key, value in raw.items():
        normalizer = DECLARED_KEYS.get(key)
        if normalizer is None:
            result.rejected[key] = "not part of the declared metadata vocabulary (unknown key)"
            continue
        normalized = normalizer(value, result.rejected)  # type: ignore[operator]
        if normalized is not None:
            result.declared[key] = normalized
    return result


@dataclass
class StampedMetadata:
    """The record-keeper's view of one stamping: what was declared, what was rejected, and
    the materialized verdict the grader computed from exactly the declared dict."""

    claim_id: uuid.UUID
    declared: dict
    rejected: dict[str, str]
    assessment: EvidenceQualityAssessment
    declared_via: str
    recorded_at: datetime

    @property
    def grade(self) -> QualityGrade:
        return self.assessment.grade

    @property
    def cap(self) -> EpistemicStatus:
        return self.assessment.epistemic_cap


class ClaimNotFoundError(LookupError):
    """The claim does not exist for this owner (or is not visible to it)."""


class NothingDeclaredError(ValueError):
    """A stamping that would record zero declared keys is refused: writing a row of
    nothing-but-rejections would materialize the `unassessable` verdict of an EMPTY
    declaration — an honest absence must stay absent, not become a stored grade that
    looks like a founder decision."""


def record_claim_evidence_metadata(
    db: Session,
    owner_id: uuid.UUID,
    claim_id: uuid.UUID,
    raw_metadata: dict | None,
    *,
    declared_via: str,
) -> StampedMetadata:
    """Validate visibility, normalize fail-closed, grade ONCE, upsert the materialized row.

    Owner-scoped by query AND by the composite FK: a claim_id from another owner 404s here
    and would fail the FK even if the query missed. The caller's session carries the RLS
    GUC (get_db sets it per request); this function does not touch session state.

    Re-stamping an existing row UPDATES it in place — a corrected declaration must move
    the materialized verdict with it, never leave a stale grade behind."""
    claim = (
        db.query(KnowledgeClaim)
        .filter(KnowledgeClaim.id == claim_id, KnowledgeClaim.owner_id == owner_id)
        .first()
    )
    if claim is None:
        raise ClaimNotFoundError(f"claim {claim_id} not found for this owner")

    if not isinstance(declared_via, str) or not declared_via.strip():
        raise ValueError("declared_via provenance is required (who declared this metadata)")
    declared_via = declared_via.strip()[:DECLARED_VIA_MAX]

    normalized = normalize_declared_metadata(raw_metadata)
    if not normalized.declared:
        raise NothingDeclaredError(
            "no valid metadata keys were declared — refusing to stamp an empty declaration "
            f"(rejected: {normalized.rejected or 'nothing supplied at all'})"
        )

    assessment = grade_evidence(normalized.declared)
    now = datetime.utcnow()

    row_values = {
        "owner_id": owner_id,
        "claim_id": claim_id,
        "metadata_json": normalized.declared,
        "quality_grade": assessment.grade.value,
        "quality_cap": assessment.epistemic_cap.value,
        "quality_caveats": list(assessment.caveats),
        "axis_findings": [
            {
                "axis": f.axis,
                "direction": f.direction,
                "finding": str(f.finding),
                "reason": f.reason,
            }
            for f in assessment.axes
        ],
        "rejected_keys": normalized.rejected,
        "declared_via": declared_via,
        "recorded_at": now,
        "updated_at": now,
    }

    stmt = pg_insert(ClaimEvidenceMetadata).values(**row_values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_claim_evidence_metadata_claim_owner",
        set_={
            "metadata_json": stmt.excluded.metadata_json,
            "quality_grade": stmt.excluded.quality_grade,
            "quality_cap": stmt.excluded.quality_cap,
            "quality_caveats": stmt.excluded.quality_caveats,
            "axis_findings": stmt.excluded.axis_findings,
            "rejected_keys": stmt.excluded.rejected_keys,
            "declared_via": stmt.excluded.declared_via,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    db.execute(stmt)

    return StampedMetadata(
        claim_id=claim_id,
        declared=normalized.declared,
        rejected=normalized.rejected,
        assessment=assessment,
        declared_via=declared_via,
        recorded_at=now,
    )


def quality_metadata_for_claims(
    db: Session, owner_id: uuid.UUID, claim_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ClaimEvidenceMetadata]:
    """Batch read for per-claim verdict paths (the library detail view): ONE query for the
    whole page instead of a subquery per claim — same cost shape as the live-confidence
    recomputation it sits beside."""
    if not claim_ids:
        return {}
    rows = (
        db.query(ClaimEvidenceMetadata)
        .filter(
            ClaimEvidenceMetadata.owner_id == owner_id,
            ClaimEvidenceMetadata.claim_id.in_(claim_ids),
        )
        .all()
    )
    return {row.claim_id: row for row in rows}


_CAP_VALUES = {status.value for status in EpistemicStatus}


def cap_from_row(row: ClaimEvidenceMetadata | None) -> EpistemicStatus | None:
    """Parse the materialized cap back into the enum the ladder consumes. Unknown stored
    values fail CLOSED to None... deliberately: classify_claim treats None as "uncapped",
    which would RAISE a verdict. A corrupt cap value must never be able to promote a
    verdict, so a corrupt row is surfaced by the caller's tests instead — but promotion is
    structurally impossible here regardless of what is stored."""
    if row is None:
        return None
    if row.quality_cap in _CAP_VALUES:
        return EpistemicStatus(row.quality_cap)
    return None

def summarize_owner_evidence_landscape(
    db: Session, owner_id: uuid.UUID, *, limit: int = 50
) -> "EvidenceLandscape":
    """The chat/workbench instruction layer's landscape: re-grade THIS owner's stamped
    declarations (deterministic — same declared dict always yields the same assessment)
    and aggregate into the body-of-evidence report. Bounded (LIMIT), owner-scoped (RLS +
    explicit filter), fail-open at the call site: an empty corpus yields the honest
    "no structured evidence" landscape, never a blank check. Re-grading from the STORED
    declaration keeps provenance exact — the report describes what the founder actually
    declared, never an inferred field."""
    from app.rag.evidence_quality import EvidenceLandscape, grade_evidence, summarize_evidence_landscape

    rows = (
        db.query(ClaimEvidenceMetadata)
        .filter(ClaimEvidenceMetadata.owner_id == owner_id)
        .order_by(ClaimEvidenceMetadata.updated_at.desc())
        .limit(max(1, int(limit)))
        .all()
    )
    items = [(row.metadata_json, grade_evidence(row.metadata_json)) for row in rows]
    return summarize_evidence_landscape(items)
