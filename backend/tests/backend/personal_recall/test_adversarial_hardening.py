from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.personal_recall.adapters import IterableAdapter
from app.personal_recall.index import LocalRecallIndex, RecallIndexError
from app.personal_recall.locator import LocatorValidationError, validate_open_locator
from app.personal_recall.query import understand_query
from app.personal_recall.retrieval import LocalSemanticScorer, PersonalRecallEngine
from app.personal_recall.types import AliasBinding, AliasVerification, CompletenessState, DecisionState, IndexState, PersonalKnowledgeItem, Provenance, SourceAuthority, SourceType, VerificationState

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)


def make(i, text="tandkräm", *, owner="alice", subject="tandkräm", source=SourceType.CONVERSATION, project=None, at=NOW, until=None, authority=SourceAuthority.UNKNOWN, decision=DecisionState.UNKNOWN, verified=VerificationState.UNKNOWN, superseded_by=None, edges=None, state=IndexState.INDEXED, file_id=None, content_hash=None, metadata=None, locator=None):
    return PersonalKnowledgeItem(item_id=i, source_type=source, source_id=f"source-{i}", owner_id=owner, content_reference=f"local://{i}", provenance=Provenance(source, f"source-{i}", locator or f"local://{i}", occurred_at=at), text=text, subject=subject, project_id=project, file_id=file_id, created_at=at, valid_until=until, source_authority=authority, decision_state=decision, verification_state=verified, superseded_by=superseded_by, relationship_edges=edges or {}, index_state=state, content_hash=content_hash, metadata=metadata or {})


def recall(items, query, **kwargs):
    return PersonalRecallEngine([IterableAdapter("items", items)], **kwargs).recall(owner_id="alice", raw_query=query, now=NOW)


class MaxSemantic(LocalSemanticScorer):
    def score(self, query, item): return 1.0


@pytest.mark.parametrize("query,wrong", [("hap", "app release"), ("fluor", "fluorid behandling"), ("bil recept", "mat recept"), ("tandkräm", "tandkrämshållare")])
def test_subject_identity_rejects_substrings_short_acronyms_and_wrong_domain(query, wrong):
    assert recall([make("wrong", wrong, subject="software")], query, semantic_scorer=MaxSemantic()).results == []


def test_inflection_and_bilingual_verified_alias_work_without_globalizing_alias():
    bindings = (AliasBinding("hydroxyapatite", "hap", "alice", AliasVerification.VERIFIED, project_id="oral"), AliasBinding("hemligt", "hap", "bob", AliasVerification.VERIFIED))
    oral = PersonalRecallEngine([IterableAdapter("items", [make("oral", subject="hydroxyapatite", project="oral")])], alias_bindings=bindings).recall(owner_id="alice", raw_query="ta fram allt vi haft om hap", project_id="oral", now=NOW)
    assert [r.item.item_id for r in oral.results] == ["oral"]
    assert understand_query("tandkrämen").terms == ("tandkrämen",)


def test_observed_rejected_expired_assistant_and_wrong_project_aliases_have_no_authority():
    bindings = (
        AliasBinding("hydroxyapatite", "hap", "alice", AliasVerification.OBSERVED),
        AliasBinding("hydroxyapatite", "nano", "alice", AliasVerification.REJECTED),
        AliasBinding("hydroxyapatite", "oldhap", "alice", AliasVerification.VERIFIED, valid_until=NOW - timedelta(days=1)),
        AliasBinding("other", "hap", "alice", AliasVerification.VERIFIED, project_id="other", source_role="assistant"),
    )
    for query in ("hap", "nano", "oldhap"):
        parsed = understand_query(query, alias_bindings=bindings, owner_id="alice", project_id="oral", now=NOW)
        assert parsed.subject != "hydroxyapatite"


def test_recent_is_not_current_truth_and_future_dates_do_not_win():
    old = make("decision", authority=SourceAuthority.USER, decision=DecisionState.DECISION, verified=VerificationState.VERIFIED, at=NOW - timedelta(days=300))
    new = make("mention", authority=SourceAuthority.ASSISTANT, decision=DecisionState.MENTION, verified=VerificationState.FAILED, at=NOW - timedelta(days=1))
    future = make("future", authority=SourceAuthority.ASSISTANT, at=NOW + timedelta(days=30))
    response = recall([new, future, old], "tandkräm")
    assert response.results[0].item.item_id == "decision"
    assert next(r for r in response.results if r.item.item_id == "future").temporal_score == 0
    assert any("future-dated" in warning for warning in response.index_warnings)


def test_valid_until_future_is_current_expired_is_not_and_missing_timestamp_is_safe():
    future = make("future-valid", until=NOW + timedelta(days=1))
    expired = make("expired", until=NOW - timedelta(seconds=1))
    missing = make("missing")
    missing.created_at = None
    missing.provenance = Provenance(missing.source_type, missing.source_id, "local://missing")
    response = recall([future, expired, missing], "senaste tandkräm")
    assert {r.item.item_id for r in response.results} == {"future-valid", "missing"}


def test_supersession_missing_node_cycle_and_two_current_are_reported_boundedly():
    missing = make("missing-middle", superseded_by="gone")
    a = make("a", superseded_by="b")
    b = make("b", superseded_by="a")
    c = make("c")
    d = make("d")
    response = recall([missing, a, b, c, d], "tandkräm")
    assert {"a", "b"}.issubset(response.historical_items)
    assert any("cycle" in w for w in response.index_warnings)
    assert any("missing" in w for w in response.index_warnings)
    assert any("current candidates" in w for w in response.index_warnings)


def test_source_authority_and_truth_remain_visible_and_ranked():
    primary = make("primary", source=SourceType.FILE, authority=SourceAuthority.PRIMARY, verified=VerificationState.VERIFIED, decision=DecisionState.VERIFIED_RESULT)
    assistant = make("assistant", source=SourceType.PREVIOUS_ANSWER, authority=SourceAuthority.ASSISTANT, verified=VerificationState.UNVERIFIED, decision=DecisionState.CLAIM)
    response = recall([assistant, primary], "tandkräm")
    assert [r.item.item_id for r in response.results] == ["primary", "assistant"]
    assert response.results[0].authority_score > response.results[1].authority_score
    assert response.results[0].truth_score > response.results[1].truth_score


def test_structured_incompatibility_is_only_a_candidate_without_explicit_edge():
    yes = make("yes", metadata={"proposition": "contains fluoride", "proposition_value": True})
    no = make("no", metadata={"proposition": "contains fluoride", "proposition_value": False})
    response = recall([yes, no], "tandkräm")
    assert response.contradictions == []
    assert len(response.contradiction_candidates) == 1
    assert response.contradiction_candidates[0].confidence < 1


def test_unstructured_opposite_text_is_not_auto_declared_contradiction():
    response = recall([make("yes", "innehåller fluor"), make("no", "innehåller inte fluor")], "tandkräm")
    assert response.contradictions == response.contradiction_candidates == []


def test_dedup_requires_same_file_identity_and_verified_fresh_hash():
    common = {"content_hash": "same"}
    distinct = [make("chat1", source=SourceType.CONVERSATION, **common), make("chat2", source=SourceType.CONVERSATION, **common)]
    stale = [make("old", file_id="f", content_hash="same", metadata={"content_hash_verified": True}), make("changed", file_id="f", content_hash="same")]
    response = recall([*distinct, *stale], "tandkräm")
    assert len(response.results) == 4


def test_verified_rename_dedups_but_never_across_owner():
    meta = {"content_hash_verified": True}
    alice = [make("one", file_id="f", content_hash="same", metadata=meta), make("rename", file_id="f", content_hash="same", metadata=meta)]
    bob = make("bob", owner="bob", file_id="f", content_hash="same", metadata=meta)
    response = recall([*alice, bob], "tandkräm")
    assert len(response.results) == 1
    assert response.results[0].related_items == ("rename",)


def test_snapshot_rejects_corruption_wrong_owner_version_symlink_and_escape(tmp_path: Path):
    path = tmp_path / "index.json"
    index = LocalRecallIndex(owner_id="alice")
    index.replace([make("safe")])
    index.save(path, base_dir=tmp_path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(RecallIndexError, match="owner"):
        LocalRecallIndex.load(path, owner_id="bob")
    payload = json.loads(path.read_text())
    payload["format_version"] = 99
    path.write_text(json.dumps(payload))
    with pytest.raises(RecallIndexError, match="format"):
        LocalRecallIndex.load(path, owner_id="alice")
    path.write_text("{")
    with pytest.raises(RecallIndexError, match="corrupt"):
        LocalRecallIndex.load(path)
    target = tmp_path / "target"
    target.write_text("x")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(RecallIndexError, match="symlink"):
        LocalRecallIndex.load(link)
    with pytest.raises(RecallIndexError, match="escapes"):
        index.save(tmp_path.parent / "escape.json", base_dir=tmp_path)


def test_snapshot_integrity_stale_writer_lock_and_deleted_restart(tmp_path: Path):
    path = tmp_path / "index.json"
    current = LocalRecallIndex(owner_id="alice")
    current.replace([make("deleted", state=IndexState.DELETED)])
    current.save(path)
    stale = LocalRecallIndex(owner_id="alice", generation=0)
    stale.replace([make("old")])
    current.save(path)
    with pytest.raises(RecallIndexError, match="stale"):
        stale.save(path)
    lock = path.with_suffix(".json.lock")
    lock.write_text("")
    with pytest.raises(RecallIndexError, match="progress"):
        current.save(path)
    lock.unlink()
    loaded = LocalRecallIndex.load(path, owner_id="alice")
    assert recall(list(loaded.items.values()), "tandkräm").results == []


@pytest.mark.parametrize("raw,intent", [("vad vare vi kom fram till med tandkrämen nu igen", "decision_history"), ("vilken va senaste", "latest_state"), ("ta fram allt vi haft om hap", "broad_recall"), ("vad ändra vi sist", "change_history"), ("varför tog vi bort den", "why_changed"), ("pdfen jag skicka förut", "file_lookup")])
def test_dennis_style_query_intents(raw, intent):
    assert intent in {value.value for value in understand_query(raw).intents}


def test_model_interpretation_cannot_be_passed_as_retrieval_authority():
    with pytest.raises(TypeError):
        understand_query("den där gamla skiten", model_result={"subject": "secret"})


def test_completeness_states_never_equate_search_finished_with_all_found():
    complete = PersonalRecallEngine([IterableAdapter("chat", [make("a")], source_types=[SourceType.CONVERSATION])], expected_source_types=[SourceType.CONVERSATION]).recall(owner_id="alice", raw_query="allt om tandkräm", now=NOW)
    unknown = recall([make("a")], "allt om tandkräm")
    partial = PersonalRecallEngine([IterableAdapter("chat", [make("a")])], expected_source_types=[SourceType.CONVERSATION, SourceType.FILE]).recall(owner_id="alice", raw_query="allt om tandkräm", now=NOW)
    assert complete.coverage.state == CompletenessState.COMPLETE
    assert unknown.coverage.state == CompletenessState.UNKNOWN
    assert partial.coverage.state == CompletenessState.UNKNOWN
    assert partial.coverage.missing_source_types == (SourceType.FILE,)
    assert "Coverage: unknown" in unknown.synthesis


def test_adapter_failure_and_partial_index_are_known_partial():
    class Failure:
        name = "files"
        source_types = {SourceType.FILE}
        def discover(self, *, owner_id): raise RuntimeError("offline")
    response = PersonalRecallEngine([Failure(), IterableAdapter("chat", [make("partial", state=IndexState.PARTIAL)])], expected_source_types=[SourceType.FILE, SourceType.CONVERSATION]).recall(owner_id="alice", raw_query="allt om tandkräm", now=NOW)
    assert response.coverage.state == CompletenessState.KNOWN_PARTIAL
    assert response.coverage.failed_adapters == ("files",)


@pytest.mark.parametrize("locator", ["https://evil.test/x", "file://../secret", "local://a/../secret", "ftp://safe", "local://wrong"])
def test_locator_rejects_network_traversal_unsupported_and_stale_identity(locator):
    with pytest.raises(LocatorValidationError):
        validate_open_locator(make("safe", locator=locator), owner_id="alice")


def test_locator_rejects_foreign_deleted_stale_but_accepts_inert_local():
    assert validate_open_locator(make("safe"), owner_id="alice").source_id == "safe"
    for value in (make("foreign", owner="bob"), make("deleted", state=IndexState.DELETED), make("stale", state=IndexState.STALE)):
        with pytest.raises(LocatorValidationError):
            validate_open_locator(value, owner_id="alice")


def test_repr_redacts_personal_content_and_nonlocal_semantic_hook_is_rejected():
    secret = make("secret", "my private medical secret", metadata={"private": "token"})
    assert "medical" not in repr(secret) and "token" not in repr(secret)
    response = recall([secret], "my private medical query")
    assert "medical" not in repr(response)
    with pytest.raises(ValueError, match="local"):
        PersonalRecallEngine([], semantic_scorer=lambda q, i: 1.0)


def test_bounded_items_results_text_aliases_and_duplicate_storm(monkeypatch):
    monkeypatch.setattr(PersonalRecallEngine, "MAX_ITEMS", 20)
    monkeypatch.setattr(PersonalRecallEngine, "MAX_RESULTS", 5)
    items = [make(str(i), "tandkräm" + "x" * 200_000, file_id=str(i), content_hash="same") for i in range(100)]
    response = recall(items, "tandkräm")
    assert len(response.results) == 5
    assert response.coverage.truncated
    assert response.coverage.state == CompletenessState.KNOWN_PARTIAL


def test_alias_registry_has_hard_bound():
    binding = AliasBinding("x", "y", "alice", AliasVerification.OBSERVED)
    with pytest.raises(ValueError, match="bound"):
        PersonalRecallEngine([], alias_bindings=[binding] * 10_001)
