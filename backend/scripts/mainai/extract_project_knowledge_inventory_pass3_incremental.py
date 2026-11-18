"""Pass-3 incremental re-scan for the LifeAI project-knowledge inventory (founder directive,
2026-11-15, "CONTINUE ACTIVE ENGINEERING", Priority 3). See
docs/LIFE_PROJECT_KNOWLEDGE_INGESTION_DESIGN.md §2 for the design this implements.

STILL RECONNAISSANCE, NOT INGESTION -- identical posture to Pass 1/Pass 2: writes no database
row of any kind, creates no InterpretationProposal/ProjectEntity/WorkCandidate. The only thing
this pass adds is NOT reprocessing the whole corpus on every run.

Mechanism: load the previous run's output (Pass 2's JSON, or this script's own prior output on
a later re-run), group its records by source_file, and compare each file's previously-recorded
source_last_commit against its CURRENT `git log -1` commit (the exact same primitive Pass 2
already uses, imported here, not reimplemented):
  - unchanged commit -> that file's records are carried forward byte-for-byte, never
    re-extracted (this is the whole incremental saving).
  - changed commit, or a file not seen in the previous run -> re-extracted in full for that
    ONE file, using Pass 1's own PATTERNS and Pass 2's own enrichment functions (imported, not
    duplicated).
  - a file present in the previous run but no longer in the current corpus scan -> its old
    records are kept, each flagged source_file_removed=true, rather than silently dropped.

Usage: python3 backend/scripts/mainai/extract_project_knowledge_inventory_pass3_incremental.py
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PASS2_PATH = REPO_ROOT / "docs" / "LIFE_PROJECT_KNOWLEDGE_INVENTORY_PASS2.json"
OUTPUT_PATH = REPO_ROOT / "docs" / "LIFE_PROJECT_KNOWLEDGE_INVENTORY_PASS3.json"


def _load_sibling_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


pass1 = _load_sibling_module("_p3_pass1", "extract_project_knowledge_inventory_pass1.py")
pass2 = _load_sibling_module("_p3_pass2", "extract_project_knowledge_inventory_pass2.py")


def _extract_one_file(path: Path) -> list[dict]:
    """The exact per-line classification loop Pass 1's extract() runs over the WHOLE corpus,
    applied here to a single file -- imported PATTERNS, never a second copy of the regexes."""
    rel = str(path.relative_to(REPO_ROOT))
    records: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return records
    for i, line in enumerate(lines, start=1):
        for strategy, pattern, entity_type, status in pass1.PATTERNS:
            m = pattern.match(line) if strategy.startswith("checkbox") or strategy == "status_declaration_marker" else pattern.search(line)
            if not m:
                continue
            text = m.group(1).strip() if strategy.startswith("checkbox") else line.strip()
            records.append(
                {"source_file": rel, "line_no": i, "proposed_entity_type": entity_type, "classifier_strategy": strategy, "inferred_status": status, "text": text[:300]}
            )
            break
    return records


def _enrich(records: list[dict], *, commit: str | None) -> list[dict]:
    enriched = []
    for r in records:
        enriched.append(
            {
                **r,
                "record_id": pass2._record_id(r),
                "confidence": pass2._CONFIDENCE_BY_STRATEGY.get(r["classifier_strategy"], "unknown"),
                "source_last_commit": commit,
                "references": pass2._extract_references(r["text"]),
                "candidate_module": pass2._candidate_module(r["text"]) if r["proposed_entity_type"] == "requirement" else None,
                "promoted": False,
                "source_file_removed": False,
            }
        )
    return enriched


def main() -> None:
    # Chain against THIS script's own previous output once one exists, so a third, fourth,
    # ... run keeps comparing against the most recent state rather than always re-diffing
    # against Pass 2's one-time snapshot -- genuine incrementality across many runs, not just
    # a single Pass-2-to-Pass-3 step. Falls back to Pass 2's JSON only on the very first run.
    previous_path = OUTPUT_PATH if OUTPUT_PATH.exists() else PASS2_PATH
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {"records": []}
    previous_by_file: dict[str, list[dict]] = {}
    for r in previous["records"]:
        previous_by_file.setdefault(r["source_file"], []).append(r)
    # Tracked for EVERY scanned file, including ones with zero matching records -- without
    # this, a file with no matches would have no entry to compare against and would be
    # "rescanned" (cheaply, producing nothing) on every single run forever, inflating the
    # rescan count with files that never actually changed. Falls back to deriving it from
    # records (Pass 2's own JSON, which predates this field) when a run's own "file_commits"
    # key is absent.
    previous_commit_by_file: dict[str, str | None] = previous.get("file_commits") or {
        source_file: recs[0].get("source_last_commit") for source_file, recs in previous_by_file.items() if recs
    }

    current_files = pass1.corpus_files()
    current_rel_paths = {str(f.relative_to(REPO_ROOT)) for f in current_files}

    all_records: list[dict] = []
    reused_files: list[str] = []
    rescanned_files: list[str] = []
    current_commit_by_file: dict[str, str | None] = {}

    for path in current_files:
        rel = str(path.relative_to(REPO_ROOT))
        current_commit = pass2._git_last_commit(rel)
        current_commit_by_file[rel] = current_commit

        if rel in previous_commit_by_file and previous_commit_by_file[rel] == current_commit:
            # Equality holds for "both real, identical commits" AND "both None" (an uncommitted
            # file, or a file with zero matches, that is unchanged since the previous run) --
            # a file going from committed to uncommitted again is not a real git scenario, so
            # None==None here only ever means "no new commit since last time we looked."
            # Unchanged since the previous run -- carry forward its records (zero or more)
            # verbatim, never re-extracted.
            all_records.extend(previous_by_file.get(rel, []))
            reused_files.append(rel)
            continue

        rescanned_files.append(rel)
        raw = _extract_one_file(path)
        all_records.extend(_enrich(raw, commit=current_commit))

    removed_files = sorted(set(previous_commit_by_file) - current_rel_paths)
    for rel in removed_files:
        for r in previous_by_file.get(rel, []):
            all_records.append({**r, "source_file_removed": True})

    by_confidence: dict[str, int] = {}
    for r in all_records:
        by_confidence[r.get("confidence", "unknown")] = by_confidence.get(r.get("confidence", "unknown"), 0) + 1

    out = {
        "pass": 3,
        "incremental": True,
        "files_in_corpus": len(current_files),
        "files_reused_unchanged": len(reused_files),
        "files_rescanned": len(rescanned_files),
        "files_removed_since_previous_run": len(removed_files),
        "rescanned_files": rescanned_files,
        "removed_files": removed_files,
        "records_total": len(all_records),
        "by_confidence": by_confidence,
        # Persisted for EVERY corpus file (including zero-match ones) so the NEXT run can
        # skip them too -- see the "previous_commit_by_file" comment above for why this field
        # exists at all.
        "file_commits": current_commit_by_file,
        "records": all_records,
    }
    OUTPUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "records"}, indent=2, ensure_ascii=False))
    print(f"\nWrote {len(all_records)} records ({len(reused_files)} files reused unchanged, {len(rescanned_files)} files rescanned) to {OUTPUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
