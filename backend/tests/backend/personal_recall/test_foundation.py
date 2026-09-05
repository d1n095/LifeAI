from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.personal_recall.adapters import IterableAdapter
from app.personal_recall.index import LocalRecallIndex, SnapshotStoragePolicy
from app.personal_recall.query import understand_query
from app.personal_recall.retrieval import LocalSemanticScorer, PersonalRecallEngine
from app.personal_recall.types import AliasBinding, AliasVerification, DecisionState, IndexState, PersonalKnowledgeItem, Provenance, SourceType, VerificationState

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)


def item(i: str, text: str, *, owner: str = "alice", source=SourceType.CONVERSATION, subject="tandkräm", created="2026-01-01", decision=DecisionState.MENTION, verified=VerificationState.UNVERIFIED, superseded_by=None, edges=None, state=IndexState.INDEXED, content_hash=None, file_id=None, metadata=None):
    at = datetime.fromisoformat(created).replace(tzinfo=timezone.utc)
    return PersonalKnowledgeItem(item_id=i, source_type=source, source_id=f"source-{i}", owner_id=owner, content_reference=f"local://{i}", provenance=Provenance(source_type=source, source_id=f"source-{i}", locator=f"local://{i}", occurred_at=at), text=text, subject=subject, created_at=at, decision_state=decision, verification_state=verified, superseded_by=superseded_by, relationship_edges=edges or {}, index_state=state, content_hash=content_hash, file_id=file_id, metadata=metadata or {})


class FixedSemantic(LocalSemanticScorer):
    def __init__(self, value): self.value = value
    def score(self, query, value): return self.value


def engine(*items, aliases=None, semantic=None):
    bindings = tuple(AliasBinding(canonical, alias, "alice", AliasVerification.VERIFIED) for canonical, values in (aliases or {}).items() for alias in values)
    return PersonalRecallEngine([IterableAdapter("fixture", items)], alias_bindings=bindings, semantic_scorer=FixedSemantic(semantic) if semantic is not None else None)


def test_query_intents_are_explicit():
    q = understand_query("jämför gamla versionen och visa varifrån du fick det")
    assert {x.value for x in q.intents} >= {"version_compare", "source_lookup", "show_evidence"}


def test_exact_term_and_semantic_paraphrase_have_separate_scores():
    found = engine(item("a", "nano hydroxyapatit recept"), semantic=0.8).recall(owner_id="alice", raw_query="allt om hydroxyapatit", now=NOW).results[0]
    assert found.exact_score > 0
    assert found.semantic_score == 0.8
    assert found.subject_match


def test_typo_and_local_alias_are_recoverable():
    e = engine(item("a", "hydroxyapatit och mineralisering", subject="hydroxyapatit"), aliases={"hydroxyapatit": ("hap tandkräm", "nano hap")})
    assert e.recall(owner_id="alice", raw_query="allt om hap tandkräm", now=NOW).results[0].item.item_id == "a"
    assert engine(item("a", "tandkräm recept")).recall(owner_id="alice", raw_query="tandkrm", now=NOW).results


def test_semantic_score_alone_cannot_collapse_wrong_subject():
    wrong = item("car", "service och recept", subject="bil")
    assert engine(wrong, semantic=1.0).recall(owner_id="alice", raw_query="tandkräm", now=NOW).results == []


def test_latest_excludes_superseded_but_history_keeps_it():
    v1 = item("v1", "tandkräm recept x", superseded_by="v2", created="2025-01-01")
    v2 = item("v2", "tandkräm recept y", created="2026-01-01")
    latest = engine(v1, v2).recall(owner_id="alice", raw_query="senaste tandkräm", now=NOW)
    assert [r.item.item_id for r in latest.results] == ["v2"]
    all_history = engine(v1, v2).recall(owner_id="alice", raw_query="visa gamla version tandkräm", now=NOW)
    assert set(all_history.historical_items) == {"v1"}
    assert {r.item.item_id for r in all_history.results} == {"v1", "v2"}


def test_relationship_version_chain_and_timeline_are_ordered():
    v1 = item("v1", "tandkräm v1", created="2024-01-01")
    v2 = item("v2", "tandkräm v2", created="2025-01-01", edges={"supersedes": ("v1",)})
    response = engine(v2, v1).recall(owner_id="alice", raw_query="tidslinje tandkräm", now=NOW)
    assert response.clusters["tandkräm"] == ["v1", "v2"]
    assert response.historical_items == ["v1"]


def test_contradictions_are_surfaced_not_merged():
    a = item("a", "fluor ska ingå", edges={"contradicts": ("b",)})
    b = item("b", "fluor ska inte ingå", verified=VerificationState.DISPUTED)
    response = engine(a, b).recall(owner_id="alice", raw_query="motsägelser tandkräm", now=NOW)
    assert response.contradictions == [("a", "b")]
    assert set(response.unresolved) == {"a", "b"}


def test_user_correction_beats_old_assistant_claim_without_deleting_history():
    assistant = item("old", "dosen är 10 gram", decision=DecisionState.CLAIM, superseded_by="correction")
    correction = item("correction", "dosen är 5 gram", decision=DecisionState.DECISION, verified=VerificationState.VERIFIED)
    response = engine(assistant, correction).recall(owner_id="alice", raw_query="senaste tandkräm dosen", now=NOW)
    assert [r.item.item_id for r in response.results] == ["correction"]


def test_file_lookup_uses_content_not_similar_title_only():
    wrong = item("wrong", "bilförsäkring", source=SourceType.FILE, subject="bil", file_id="f1")
    right = item("right", "tandkräm med nano hap", source=SourceType.FILE, subject="tandkräm", file_id="f2")
    response = engine(wrong, right).recall(owner_id="alice", raw_query="den där PDF filen om tandkräm", now=NOW)
    assert [r.item.item_id for r in response.results] == ["right"]


def test_renamed_and_duplicate_file_handling_preserves_provenance():
    original = item("original", "tandkräm pdf", source=SourceType.FILE, file_id="f1", content_hash="abc", metadata={"content_hash_verified": True})
    renamed = item("renamed", "tandkräm pdf", source=SourceType.FILE, file_id="f1", content_hash="abc", metadata={"content_hash_verified": True})
    response = engine(original, renamed).recall(owner_id="alice", raw_query="fil tandkräm", now=NOW)
    assert len(response.results) == 1
    assert response.results[0].related_items in (("renamed",), ("original",))
    assert response.results[0].item.provenance.locator.startswith("local://")


def test_partial_deleted_and_failed_index_states_are_honest():
    partial = item("partial", "tandkräm", state=IndexState.PARTIAL)
    deleted = item("deleted", "tandkräm", state=IndexState.DELETED)
    failed = item("failed", "tandkräm", state=IndexState.FAILED)
    response = engine(partial, deleted, failed).recall(owner_id="alice", raw_query="allt om tandkräm", now=NOW)
    assert [r.item.item_id for r in response.results] == ["partial"]
    assert "partial" in response.index_warnings[0]
    assert "incomplete" in response.synthesis


def test_decision_history_is_distinct_from_casual_mentions():
    mention = item("mention", "tandkräm kanske x", decision=DecisionState.MENTION)
    decision = item("decision", "tandkräm beslut y", decision=DecisionState.DECISION)
    response = engine(mention, decision).recall(owner_id="alice", raw_query="vad kom vi fram till om tandkräm", now=NOW)
    assert {r.item.decision_state for r in response.results} == {DecisionState.MENTION, DecisionState.DECISION}
    assert "decision_history" in {i.value for i in response.query.intents}


def test_all_about_returns_multiple_source_classes():
    values = [item("chat", "tandkräm", source=SourceType.CONVERSATION), item("file", "tandkräm", source=SourceType.FILE), item("memory", "tandkräm", source=SourceType.DURABLE_MEMORY)]
    response = engine(*values).recall(owner_id="alice", raw_query="allt om tandkräm", now=NOW)
    assert {r.item.source_type for r in response.results} == {SourceType.CONVERSATION, SourceType.FILE, SourceType.DURABLE_MEMORY}


def test_no_cross_owner_leakage_even_with_broken_adapter():
    class Broken:
        name = "broken"
        def discover(self, *, owner_id):
            return [item("bob-secret", "tandkräm", owner="bob")]
    response = PersonalRecallEngine([Broken()]).recall(owner_id="alice", raw_query="tandkräm", now=NOW)
    assert response.results == []
    assert "cross-owner" in response.index_warnings[0]


def test_index_survives_reload_and_query_survives_engine_restart(tmp_path: Path):
    path = tmp_path / "recall.json"
    index = LocalRecallIndex()
    index.replace([item("durable", "tandkräm recept", source=SourceType.FILE)])
    policy = SnapshotStoragePolicy(tmp_path)
    index.save(path, policy=policy)
    loaded = LocalRecallIndex.load(path, owner_id="alice", policy=policy)
    restarted = PersonalRecallEngine([IterableAdapter("snapshot", loaded.items.values())])
    assert restarted.recall(owner_id="alice", raw_query="tandkräm", now=NOW).results[0].item.item_id == "durable"
