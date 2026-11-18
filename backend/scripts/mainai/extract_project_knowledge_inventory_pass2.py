"""Pass-2 for the LifeAI project-knowledge inventory (founder directive, 2026-11-15,
"STOP IDLING, CONTINUE ENGINEERING", Priority 2). Reads Pass 1's output
(docs/LIFE_PROJECT_KNOWLEDGE_INVENTORY_PASS1.json) and adds, computationally, what Pass 1
deliberately left as narrative-only: stable content-hash identities (for deduplication across
re-runs even when line numbers shift), per-file source-version tracking (git last-commit
hash, for detecting corpus drift since extraction), a measured confidence tier per classifier
(not asserted -- see docs/LIFE_PROJECT_KNOWLEDGE_INVENTORY_PASS2.md for the manual sample
review this is based on), extracted supersession references for the high-confidence subset,
and a best-effort, explicitly-labeled-uncertain mapping from "requirement" records to
candidate implementing backend modules.

STILL RECONNAISSANCE, NOT INGESTION: like Pass 1, this writes no database row of any kind.
Nothing here is promoted into InterpretationProposal/ProjectEntity/WorkCandidate. Confidence
tiers exist to make human review FASTER and better-triaged, not to replace it -- the founder
directive is explicit that automatic ingestion based on this extraction is not authorized.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PASS1_PATH = REPO_ROOT / "docs" / "LIFE_PROJECT_KNOWLEDGE_INVENTORY_PASS1.json"
OUTPUT_PATH = REPO_ROOT / "docs" / "LIFE_PROJECT_KNOWLEDGE_INVENTORY_PASS2.json"

# Measured, not asserted -- see docs/LIFE_PROJECT_KNOWLEDGE_INVENTORY_PASS2.md section "False
# positive / false negative assessment" for the manual-review methodology and sample behind
# these tiers. superseded_marker is "low" because "superseded"/"supersedes"/"ersätter" are
# heavily used in this corpus to describe VOCABULARY (enum values, field names, relationship
# types) rather than a specific supersession EVENT -- confirmed false-positive-heavy in a
# 25-record stratified sample, NOT auto-filtered further here because a naive stricter regex
# (tested: requiring a PR reference or date) was shown to drop genuinely important records
# (e.g. AUTH_THREAT_MODEL.md's own top-level architectural decision) along with the noise --
# trading recall for precision in a way that loses real signal. The fix is human review
# (already required by this document's own "never auto-ingest" rule), not more regex.
_CONFIDENCE_BY_STRATEGY = {
    "checkbox_pending": "high",
    "checkbox_done": "high",
    "decision_marker": "high",
    "build_block_marker": "high",
    "open_question_marker": "high",
    "status_declaration_marker": "high",
    "conflict_marker": "medium",
    "superseded_marker": "low",
}

# Signals a record MIGHT point at a specific, identifiable other item (a PR number or an
# ISO date) -- surfaced as a `references` field for human review to follow up on, never
# auto-resolved into a real ProjectEntity.supersedes_entity_id edge.
_REFERENCE_PATTERN = re.compile(r"#(\d+)|(\d{4}-\d{2}-\d{2})")

# Coarse, explicitly-uncertain keyword hints for mapping a "requirement" record's text to a
# plausible backend module -- NOT a claim of correctness; every hit is labeled
# "candidate_only" in the output precisely because a keyword match is weak evidence.
_MODULE_KEYWORDS = {
    "calendar": "app/calendar",
    "blueprint": "app/blueprint",
    "workforce": "app/workforce",
    "agent_coordination": "app/agent_coordination",
    "mainai_execution": "app/mainai_execution",
    "session_context": "app/session_context",
    "founder_memory": "app/founder_memory",
    "inspectable_memory": "app/inspectable_memory",
    "project_entities": "app/project_entities",
    "work_candidates": "app/work_candidates",
    "work_intelligence": "app/work_intelligence",
    "rag": "app/rag",
    "rls": "app/rls.py",
    "egress": "app/egress_policy",
    "provider": "app/providers",
    "autentisering": "app/account",
    "e-postverifiering": "app/account",
    "registrering": "app/account",
}


def _git_last_commit(rel_path: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", rel_path],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=False,
        )
        sha = out.stdout.strip()
        return sha or None
    except (OSError, subprocess.SubprocessError):
        return None


def _record_id(record: dict) -> str:
    """Content-hash identity -- stable across re-runs even if the SAME requirement moves to a
    different line number (e.g. an unrelated edit earlier in the file), unlike source_file +
    line_no alone. Deliberately NOT based on line_no."""
    basis = f"{record['source_file']}::{record['proposed_entity_type']}::{record['text']}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _extract_references(text: str) -> list[str]:
    refs = []
    for m in _REFERENCE_PATTERN.finditer(text):
        refs.append(m.group(1) and f"#{m.group(1)}" or m.group(2))
    return refs


def _candidate_module(text: str) -> str | None:
    lowered = text.lower()
    for keyword, module in _MODULE_KEYWORDS.items():
        if keyword in lowered:
            return module
    return None


def main() -> None:
    pass1 = json.loads(PASS1_PATH.read_text(encoding="utf-8"))
    records = pass1["records"]

    commit_cache: dict[str, str | None] = {}
    seen_ids: dict[str, int] = {}
    enriched = []
    for r in records:
        rid = _record_id(r)
        seen_ids[rid] = seen_ids.get(rid, 0) + 1  # duplicate-identity count, not deduped away

        source_file = r["source_file"]
        if source_file not in commit_cache:
            commit_cache[source_file] = _git_last_commit(source_file)

        enriched.append(
            {
                **r,
                "record_id": rid,
                "confidence": _CONFIDENCE_BY_STRATEGY.get(r["classifier_strategy"], "unknown"),
                "source_last_commit": commit_cache[source_file],
                "references": _extract_references(r["text"]),
                "candidate_module": _candidate_module(r["text"]) if r["proposed_entity_type"] == "requirement" else None,
                "promoted": False,  # never true from this script -- reconnaissance only
            }
        )

    duplicate_ids = {rid: n for rid, n in seen_ids.items() if n > 1}

    by_confidence: dict[str, int] = {}
    for r in enriched:
        by_confidence[r["confidence"]] = by_confidence.get(r["confidence"], 0) + 1

    with_references = [r for r in enriched if r["references"]]
    requirement_records = [r for r in enriched if r["proposed_entity_type"] == "requirement"]
    mapped_to_module = [r for r in requirement_records if r["candidate_module"]]

    out = {
        "pass": 2,
        "based_on_pass1_records": len(records),
        "records_extracted": len(enriched),
        "unique_record_ids": len(seen_ids),
        "duplicate_record_ids": len(duplicate_ids),
        "by_confidence": by_confidence,
        "records_with_extracted_references": len(with_references),
        "requirement_records": len(requirement_records),
        "requirement_records_with_candidate_module": len(mapped_to_module),
        "requirement_records_with_candidate_module_pct": round(100 * len(mapped_to_module) / len(requirement_records), 1) if requirement_records else 0.0,
        "records": enriched,
    }
    OUTPUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "records"}, indent=2, ensure_ascii=False))
    print(f"\nWrote {len(enriched)} enriched records to {OUTPUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
