"""Reuse-before-build gate: explicit evidence, real checkout, no authority.

These tests use the real repository scan and the real Postgres test database. They do not
assert a coverage percentage and they never treat a located module as a working capability.
"""

import uuid
from pathlib import Path

from app.mainai_executive.reuse_gate import (
    check_reuse_before_build,
    format_reuse_instructions,
    looks_like_build_request,
)
from app.models.project_memory import NoteKind
from app.project_memory import add_note


def test_build_intent_is_explicit_and_language_independent():
    assert looks_like_build_request("Kan du bygga en minnesfunktion åt mig?")
    assert looks_like_build_request("Implementera en ny chatt-endpoint")
    assert looks_like_build_request("Please build a retrieval function")
    # A question or a summary is not a build request; it must not trigger a repo scan.
    assert not looks_like_build_request("Vad är klockan?")
    assert not looks_like_build_request("Sammanfatta dokumentet jag laddade upp")


def test_plain_question_returns_none_without_scanning(db_session):
    assert (
        check_reuse_before_build(
            db_session,
            owner_id=uuid.uuid4(),
            request_text="Hur mår systemet idag?",
        )
        is None
    )


def test_swedish_memory_request_finds_the_existing_modules_and_real_refs(db_session):
    report = check_reuse_before_build(
        db_session,
        owner_id=uuid.uuid4(),
        request_text="Kan du bygga en minnesfunktion åt mig?",
    )

    assert report is not None
    assert report["read_only"] is True
    assert report["execution_authorized"] is False
    # The Swedish synonym must reach the same reviewed modules as "memory".
    assert any("memory" in module for module in report["existing_implementations"])
    assert report["outcome"] in {"reuse", "improve", "investigate"}
    assert report["components"], "expected explicit module evidence"

    for component in report["components"][:3]:
        assert component["state"] in {
            "module_located",
            "implementation_inspected",
            "tests_observed",
            "integration_verified",
            "runtime_verified",
        }
        source_path = component.get("source_path")
        if source_path:
            assert Path(source_path).is_file(), "a cited source path must exist"


def test_recorded_project_decision_is_surfaced_with_its_citation(db_session):
    note = add_note(
        db_session,
        kind=NoteKind.decision,
        content="Beslut: minnesfunktionen ska återanvändas, inte byggas om.",
        source_type="doc",
        source_ref="docs/BRANCH_REGISTRY.md#minne",
        created_by="founder@lifeos.local",
    )

    report = check_reuse_before_build(
        db_session,
        owner_id=uuid.uuid4(),
        request_text="Kan du bygga en minnesfunktion?",
    )

    assert report is not None
    decisions = report["prior_project_decisions"]
    assert any(item["content"].startswith("Beslut: minnesfunktionen") for item in decisions)
    matching = next(item for item in decisions if item["content"].startswith("Beslut: minnesfunktionen"))
    assert matching["source_ref"] == "docs/BRANCH_REGISTRY.md#minne"
    assert str(note.id)  # the note is a real persisted row, not a fabricated string


def test_prompt_block_states_the_limits_and_cites_evidence(db_session):
    report = check_reuse_before_build(
        db_session,
        owner_id=uuid.uuid4(),
        request_text="Bygg en ny dokumentfunktion",
    )
    assert report is not None

    block = format_reuse_instructions(report)
    assert "ÅTERANVÄNDNINGSKONTROLL" in block
    assert "ingen exekveringsbehörighet" in block
    assert "app." in block
    assert "Analysen är" in block
