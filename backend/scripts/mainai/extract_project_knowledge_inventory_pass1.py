"""Pass-1 reconnaissance extraction for the LifeAI project-knowledge inventory (founder
directive, 2026-11-15, "Execution Authority, Integration and Complete Project Knowledge",
Priority 3). See docs/LIFE_PROJECT_KNOWLEDGE_INVENTORY_MODEL.md for the full design — this
script implements only the mechanical extraction step that doc describes.

WHAT THIS IS: a pure, stdlib-only, no-database, no-provider scan of the docs corpus for
requirement/decision/dependency/open-question-SHAPED lines, using named, auditable regex
classifiers (see PATTERNS below) — never free-text LLM interpretation, so every record this
produces is independently re-checkable by re-running this exact script.

WHAT THIS IS NOT: this does NOT write to app.project_entities, app.work_candidates, or any
other table. It does NOT create a KnowledgeClaim, InterpretationProposal, or ProjectEntity
row. There is no Postgres or AI-provider access in the sandbox this was written in to do
so, and doing so would also require each of these documents to first exist as a real
MemorySourceUnit (Library ingestion) -- a separate, substantial, NOT-done-here step. This
script's JSON output is reconnaissance for human/founder review, and a coverage baseline a
future, real claim-extraction pass over these same documents (once ingested) can be checked
against -- it deliberately does NOT replace or parallel app.project_entities' existing
"signal producer != truth writer" governance (see that model's own module docstring): the
only path to a real ProjectEntity remains promote_interpretation_proposal(), unchanged.

Corpus scope (explicit): every docs/*.md file plus CLAUDE.md and README.md at the repo
root. Deliberately EXCLUDES docs/BRANCH_REGISTRY.md -- that file is a continuously-changing,
session-authored PR-tracking log, not stable requirements-source content; including it would
make this script's own re-run output non-stable in a way that defeats "measurable coverage."

Usage: python3 backend/scripts/mainai/extract_project_knowledge_inventory_pass1.py
Re-run any time the docs corpus changes; the output file is fully regenerated, not appended.
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_PATH = REPO_ROOT / "docs" / "LIFE_PROJECT_KNOWLEDGE_INVENTORY_PASS1.json"
EXCLUDE = {"docs/BRANCH_REGISTRY.md"}

# (classifier_strategy, pattern, proposed_entity_type, inferred_status)
PATTERNS = [
    ("checkbox_pending", re.compile(r"^\s*-\s*\[\s\]\s+(.+)"), "requirement", "not_started"),
    ("checkbox_done", re.compile(r"^\s*-\s*\[[xX]\]\s+(.+)"), "requirement", "implemented"),
    ("decision_marker", re.compile(r".*\bBeslutat\b.*", re.IGNORECASE), "decision", "decided"),
    ("build_block_marker", re.compile(r".*\bByggspärr\b.*", re.IGNORECASE), "dependency", "blocking"),
    ("open_question_marker", re.compile(r".*(öppna fråg|open question|TODO|FIXME).*", re.IGNORECASE), "open_question", "unresolved"),
    ("conflict_marker", re.compile(r".*\bKÄND\b.*(KONFLIKT|KOLLISION|LUCKA).*", re.IGNORECASE), "open_question", "contradictory"),
    ("superseded_marker", re.compile(r".*(supersedes|superseded|ersätter)\b.*", re.IGNORECASE), "decision", "superseded"),
    ("status_declaration_marker", re.compile(r"^\*\*Status:\s*(.+?)\*\*"), "decision", "declared"),
]


def corpus_files() -> list[Path]:
    files = sorted((REPO_ROOT / "docs").glob("*.md"))
    for extra in ("CLAUDE.md", "README.md"):
        p = REPO_ROOT / extra
        if p.exists():
            files.append(p)
    return [f for f in files if str(f.relative_to(REPO_ROOT)) not in EXCLUDE]


def extract() -> tuple[list[dict], int, int]:
    records: list[dict] = []
    total_lines_scanned = 0
    for path in corpus_files():
        rel = str(path.relative_to(REPO_ROOT))
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        total_lines_scanned += len(lines)
        for i, line in enumerate(lines, start=1):
            for strategy, pattern, entity_type, status in PATTERNS:
                m = pattern.match(line) if strategy.startswith("checkbox") or strategy == "status_declaration_marker" else pattern.search(line)
                if not m:
                    continue
                text = m.group(1).strip() if strategy.startswith("checkbox") else line.strip()
                records.append(
                    {
                        "source_file": rel,
                        "line_no": i,
                        "proposed_entity_type": entity_type,
                        "classifier_strategy": strategy,
                        "inferred_status": status,
                        "text": text[:300],
                    }
                )
                break  # first matching pattern wins per line -- never double-count a line
    return records, total_lines_scanned, len(corpus_files())


def main() -> None:
    records, total_lines, n_files = extract()
    by_entity_type: dict[str, int] = {}
    by_strategy: dict[str, int] = {}
    for r in records:
        by_entity_type[r["proposed_entity_type"]] = by_entity_type.get(r["proposed_entity_type"], 0) + 1
        by_strategy[r["classifier_strategy"]] = by_strategy.get(r["classifier_strategy"], 0) + 1

    out = {
        "pass": 1,
        "corpus_files_scanned": n_files,
        "corpus_lines_scanned": total_lines,
        "records_extracted": len(records),
        "by_entity_type": by_entity_type,
        "by_strategy": by_strategy,
        "records": records,
    }
    OUTPUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "records"}, indent=2, ensure_ascii=False))
    print(f"\nWrote {len(records)} records to {OUTPUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
