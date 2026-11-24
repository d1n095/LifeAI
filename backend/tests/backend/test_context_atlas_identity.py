"""Context Atlas acceptance — source identity (Workstream G, cases 1-5, plus recovery).

These are pure-function tests: no database, no storage backend, no network.
"""

import pytest

from app.context_atlas.identity import MediaKind, Verdict, compare_sources, fingerprint
from app.context_atlas.relationships import EvidenceKind, propose_relationship


def test_byte_identical_sources_are_authoritative_and_both_originals_kept():
    payload = "Avtal mellan LifeAI och leverantören.\nBilaga 1.".encode()
    result = compare_sources(payload, bytes(payload))

    assert result.verdict is Verdict.exact_byte_identical
    assert result.authoritative is True
    assert result.similarity == 1.0
    assert result.edit_distance == 0
    assert result.requires_review is False
    assert result.proposed_relationship == "duplicates"
    # Both originals are still described in full — nothing is discarded.
    assert result.preserve_originals is True
    assert result.left.sha256 == result.right.sha256
    assert result.left.size_bytes == result.right.size_bytes


def test_one_character_change_is_not_identity():
    result = compare_sources(b"Total kostnad: 1000 kr", b"Total kostnad: 1001 kr")

    assert result.verdict is Verdict.one_character_edit
    assert result.authoritative is False
    assert result.edit_distance == 1
    assert result.requires_review is True
    assert result.proposed_relationship is None
    assert result.left.sha256 != result.right.sha256
    assert result.preserve_originals is True


def test_whitespace_only_difference_is_normalized_not_identical():
    result = compare_sources(
        "Rad ett.\n\nRad   två.\n".encode(), "Rad ett. Rad två.".encode()
    )

    assert result.verdict is Verdict.normalized_identical
    assert result.authoritative is False
    assert "whitespace_only" in result.differences
    assert result.requires_review is True
    # Normalized equality is a lead, not a dedup decision.
    assert result.proposed_relationship is None


def test_unicode_composition_difference_is_normalized():
    composed = "Café".encode()  # é as one code point
    decomposed = "Cafe\u0301".encode()  # e + combining acute
    result = compare_sources(composed, decomposed)

    assert result.verdict is Verdict.normalized_identical
    assert "unicode_equivalence_only" in result.differences
    assert result.authoritative is False


def test_similarity_is_distinguished_from_identity():
    left = (
        "LifeAI ska bevara original, spåra proveniens och aldrig slå ihop två källor "
        "enbart för att de liknar varandra. Beslut kräver grundarens godkännande."
    ).encode()
    right = (
        "LifeAI ska bevara original, spåra proveniens och aldrig slå ihop två källor "
        "enbart för att de liknar varandra. Beslut kräver grundarens godkännande idag."
    ).encode()
    result = compare_sources(left, right)

    assert result.verdict is not Verdict.exact_byte_identical
    assert result.verdict in (Verdict.near_duplicate, Verdict.similar)
    assert result.authoritative is False
    assert result.similarity is not None and result.similarity > 0.4
    assert result.proposed_relationship is None


def test_distinct_documents_are_reported_as_distinct():
    result = compare_sources(
        b"Kvartalsrapport for byggprojektet norr.", b"Recept pa kanelbullar."
    )

    assert result.verdict is Verdict.distinct
    assert result.authoritative is False
    assert result.similarity is not None and result.similarity < 0.4


def test_binary_and_invalid_utf8_are_not_comparable_but_hash_is_available():
    binary = compare_sources(b"\x00\x01\x02", b"\x00\x01\x03")
    assert binary.verdict is Verdict.not_comparable
    assert binary.left.media_kind is MediaKind.binary
    assert len(binary.left.sha256) == 64

    invalid = compare_sources(b"\xff\xfe\xfa", b"\xff\xfe\xfb")
    assert invalid.verdict is Verdict.not_comparable
    assert invalid.left.media_kind is MediaKind.undecodable


def test_oversized_input_skips_text_metrics_without_skipping_identity():
    same = b"x" * 100
    result = compare_sources(same, same, max_bytes=10)
    assert result.verdict is Verdict.exact_byte_identical

    different = compare_sources(b"x" * 100, b"y" * 100, max_bytes=10)
    assert different.verdict is Verdict.not_comparable
    assert different.left.media_kind is MediaKind.oversized


def test_empty_payloads_are_identical_and_typed_as_text():
    result = compare_sources(b"", b"")
    assert result.verdict is Verdict.exact_byte_identical
    assert result.left.media_kind is MediaKind.text


def test_fingerprint_rejects_non_bytes():
    with pytest.raises(TypeError):
        fingerprint("not bytes")  # type: ignore[arg-type]


def test_duplicates_edge_requires_exact_byte_identity_evidence():
    exact = propose_relationship("DUPLICATES", evidence=EvidenceKind.exact_byte_identity)
    similar = propose_relationship("DUPLICATES", evidence=EvidenceKind.ai_similarity)

    assert exact.existing_type == "duplicates"
    assert exact.recordable is True
    assert exact.execution_authorized is False

    assert similar.recordable is False
    assert similar.requires_review is True
    assert similar.execution_authorized is False
