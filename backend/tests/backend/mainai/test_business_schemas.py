"""Unit coverage for the model-output parser and the deterministic validators."""

from __future__ import annotations

import pytest

from app.mainai_business.schemas import SchemaValidationError, parse_capability_output
from app.mainai_business.validators import validate_output


def test_parser_tolerates_fences_and_prose():
    raw = 'Sure!\n```json\n{"demand": "high", "note": "strong"}\n```\nhope that helps'
    out = parse_capability_output("market_research", raw)
    assert out["demand"] == "high"
    assert out["capability"] == "market_research"
    assert out["segments"] == []  # default applied


def test_parser_rejects_wrong_enum():
    with pytest.raises(SchemaValidationError):
        parse_capability_output("market_research", '{"demand": "enormous", "note": "x"}')


def test_parser_rejects_missing_required_field():
    with pytest.raises(SchemaValidationError):
        parse_capability_output("unit_economics", '{"margin_note": "no viable field"}')


def test_parser_rejects_non_json():
    with pytest.raises(SchemaValidationError):
        parse_capability_output("fulfillment_ops", "I cannot answer that.")


def test_validator_blocks_go_when_finance_not_viable():
    data = {"recommendation": "go", "reason": "optimistic"}
    inputs = {"unit_economics": {"viable": False}, "fulfillment_ops": {"feasible": True}}
    ok, reason = validate_output("opportunity_synthesis", data, inputs)
    assert ok is False
    assert "not viable" in reason


def test_validator_allows_consistent_recommendation():
    data = {"recommendation": "conditional-go", "reason": "viable with mitigations"}
    inputs = {"unit_economics": {"viable": True}, "fulfillment_ops": {"feasible": True}}
    ok, _ = validate_output("opportunity_synthesis", data, inputs)
    assert ok is True


def test_publication_validator_requires_matching_recommendation():
    data = {"title": "t", "recommendation": "no-go", "summary": "s"}
    inputs = {"opportunity_synthesis": {"recommendation": "conditional-go"}}
    ok, reason = validate_output("assessment_publication", data, inputs)
    assert ok is False
