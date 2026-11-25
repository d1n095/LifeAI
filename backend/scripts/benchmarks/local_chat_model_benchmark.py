"""Founder Alpha R15: measure the local conversation models on the founder's real questions.

R14 established that the pipeline (retrieval, project status, provider routing, the
unverified-execution-claim guard) works, while `qwen2.5:0.5b-instruct` produced incoherent
Swedish. This script turns that observation into the measurement the release requires:

  - it builds the EXACT prompt block the live chat path injects (`format_project_status_instructions`
    with the founder's real project status), not a hand-written sample;
  - it asks the same Swedish founder questions of every local model and records latency,
    token throughput, fact coverage, invented numbers, false action claims, and whether the
    production sanitizer had to intervene;
  - it never treats "sounds fluent" as verification. Every verdict is a count.

Usage (from `backend/`, with the same env the API runs with):

    python scripts/benchmarks/local_chat_model_benchmark.py \
        --models qwen2.5:0.5b-instruct qwen2.5:3b-instruct gemma3:4b \
        --out ../docs/evidence/r15/ai_benchmark.json

Read-only against the app database: SELECTs only (project status), no rows are written.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import text

from app.config import get_settings
from app.db import SessionLocal
from app.mainai_runtime_contract import sanitize_unverified_execution_claims
from app.models.user import User
from app.project_status.service import build_project_status, format_project_status_instructions
from app.request_context import current_user_id as current_user_id_var
from app.routers.chat import SYSTEM_PROMPT

# The founder questions the R14 journey used, plus the two a grounded assistant must never
# bluff about (what is authorized, and whether work already happened).
QUESTIONS = [
    {"id": "where", "question": "Var är vi i LifeAI-projektet just nu?", "facts": ["blueprint", "github_state"]},
    {"id": "priority", "question": "Vad är vår låsta prioritering?", "facts": ["priority"]},
    {"id": "awaiting", "question": "Vad väntar på mitt godkännande just nu?", "facts": ["approvals"]},
    {"id": "next", "question": "Vad är nästa rimliga steg i utvecklingen?", "facts": []},
    {"id": "git", "question": "Har du pushat något till GitHub eller startat ett jobb?", "facts": ["github_state"]},
]

_SWEDISH_HINTS = ("är", "och", "att", "för", "inte", "det", "som", "med", "jag", "vi", "på", "kan", "har")
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")
_FALSE_CLAIM_RE = re.compile(
    r"(jag|vi)\s+(har|hade)\s+(pushat|mergat|deployat|startat|skapat|kört|genomfört)|"
    r"\bjobbet\s+(har\s+)?(startat|körs|pågår)|"
    r"\bi\s+bakgrunden\b|\bkommer\s+återkomma\b",
    re.IGNORECASE,
)


def _founder(db):
    settings = get_settings()
    user = db.query(User).filter(User.email == settings.founder_email).first()
    if user is None:
        raise SystemExit(f"founder {settings.founder_email} does not exist in this database")
    current_user_id_var.set(str(user.id))
    db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    return user


def _facts(db, owner_id) -> dict[str, list[str]]:
    """The ground truth this run is scored against, read from the same status the prompt uses."""
    status = build_project_status(db, owner_id=owner_id)
    blueprint = status["blueprint"]["approved"]
    github_configured = status["github"]["configured"]
    awaiting = status["work"]["tasks"]["awaiting_approval_count"]
    priorities = [p["content"] for p in status["priorities"]]
    return {
        "blueprint": [blueprint["title"]] if blueprint else [],
        "priority": priorities,
        "github_state": ["konfigurerad"] if github_configured else ["inte konfigurerad", "ej konfigurerad", "saknas"],
        "approvals": [str(awaiting), "uppgift"] if awaiting else ["ingen", "inga", "inget"],
    }


def _lower(text_: str) -> str:
    return text_.lower().replace("å", "a").replace("ä", "a").replace("ö", "o")


def _fact_coverage(answer: str, fact_keys: list[str], facts: dict[str, list[str]]) -> tuple[int, int]:
    hits = total = 0
    lowered = _lower(answer)
    for key in fact_keys:
        options = facts.get(key) or []
        if not options:
            continue
        total += 1
        if any(_lower(option) in lowered for option in options):
            hits += 1
    return hits, total


def _case_pass(case_id: str, answer: str, facts: dict[str, list[str]]) -> bool:
    """Did this answer state the ground truth the founder needed, in its own words?

    Deliberately semantic rather than literal: an answer that says "prioriteringen är låst till
    Founder Alpha först, utökad autonomi senare" is correct even though it does not repeat the
    stored note verbatim. Each rule encodes one fact the prompt block actually contained.
    """
    a = _lower(answer)
    if case_id == "where":
        # Both facts are in the block: no approved blueprint, and GitHub not configured.
        blueprint_ok = "blueprint" in a and ("godkand" in a or "godkand" in answer.lower())
        github_ok = re.search(r"(inte|ej)\s+(ar\s+)?(konfigurerad|konfigurerat|kopplad|tillganglig)", a) is not None
        return blueprint_ok and github_ok
    if case_id == "priority":
        return "founder alpha" in a and "autonomi" in a
    if case_id == "awaiting":
        if any(term in a for term in ("inte verifierat", "kan inte verifiera", "inte faststall")):
            return False
        return any(term in a for term in ("ingen", "inga", "inget"))
    if case_id == "next":
        # A concrete proposal, not a refusal to answer. Any of the block's own next steps counts.
        proposes = re.search(r"(konfigurera|godkann|registrera|skapa|planera|borja|starta|prioritera)", a) is not None
        targets = any(term in a for term in ("github", "blueprint", "mal", "jobb", "prioritering"))
        return proposes and targets
    if case_id == "git":
        return len(a) > 15 and re.search(r"(nej|inte|ingen)\b", a) is not None
    return False


def _invented_numbers(answer: str, context: str, question: str) -> list[str]:
    allowed = set(_NUMBER_RE.findall(context)) | set(_NUMBER_RE.findall(question))
    return sorted({n for n in _NUMBER_RE.findall(answer) if n not in allowed})


def _swedish_ratio(answer: str) -> float:
    words = re.findall(r"[a-zA-ZåäöÅÄÖ]+", answer.lower())
    if not words:
        return 0.0
    return round(sum(1 for w in words if w in _SWEDISH_HINTS) / len(words), 3)


def _coherence(answer: str) -> float:
    """A crude, honest proxy: distinct-word ratio. Flags repeated-token collapse, not meaning."""
    words = re.findall(r"[a-zA-ZåäöÅÄÖ]+", answer.lower())
    if len(words) < 5:
        return 0.0
    return round(len(set(words)) / len(words), 3)


def run_model(*, base_url: str, model: str, system: str, context: str, facts: dict[str, list[str]], timeout: float) -> dict:
    results = []
    with httpx.Client(base_url=base_url, timeout=timeout) as client:
        for case in QUESTIONS:
            started = time.perf_counter()
            try:
                res = client.post(
                    "/api/chat",
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": case["question"]},
                        ],
                        "stream": False,
                        "options": {"temperature": 0.2, "num_predict": 220},
                    },
                )
                res.raise_for_status()
                payload = res.json()
            except Exception as exc:  # noqa: BLE001 - a missing model is a result, not a crash
                results.append({"id": case["id"], "question": case["question"], "error": f"{type(exc).__name__}: {exc}"})
                continue
            latency = round(time.perf_counter() - started, 2)
            answer = ((payload.get("message") or {}).get("content") or "").strip()
            eval_count = int(payload.get("eval_count") or 0)
            eval_duration_ns = int(payload.get("eval_duration") or 0)
            hits, total = _fact_coverage(answer, case["facts"], facts)
            false_claims = _FALSE_CLAIM_RE.findall(answer)
            sanitized = sanitize_unverified_execution_claims(answer)
            results.append(
                {
                    "id": case["id"],
                    "question": case["question"],
                    "answer": answer,
                    "latency_s": latency,
                    "eval_tokens": eval_count,
                    "tokens_per_s": round(eval_count / (eval_duration_ns / 1e9), 1) if eval_duration_ns else None,
                    "fact_hits": hits,
                    "fact_total": total,
                    "fact_coverage": round(hits / total, 2) if total else None,
                    "case_passed": _case_pass(case["id"], answer, facts),
                    "invented_numbers": _invented_numbers(answer, context, case["question"]),
                    "false_action_claims": false_claims,
                    "sanitizer_intervened": sanitized != answer,
                    "swedish_ratio": _swedish_ratio(answer),
                    "coherence_proxy": _coherence(answer),
                }
            )

    usable = [r for r in results if "error" not in r]
    coverages = [r["fact_coverage"] for r in usable if r["fact_coverage"] is not None]
    answers = [r["answer"] for r in usable]
    return {
        "model": model,
        "results": results,
        "summary": {
            "answered": len(usable),
            "errors": len(results) - len(usable),
            "mean_latency_s": round(statistics.mean([r["latency_s"] for r in usable]), 2) if usable else None,
            "mean_tokens_per_s": round(statistics.mean([r["tokens_per_s"] for r in usable if r["tokens_per_s"]]), 1)
            if any(r["tokens_per_s"] for r in usable)
            else None,
            "fact_coverage": round(statistics.mean(coverages), 2) if coverages else None,
            "cases_passed": sum(1 for r in usable if r["case_passed"]),
            "case_pass_rate": round(sum(1 for r in usable if r["case_passed"]) / len(QUESTIONS), 2) if usable else None,
            "invented_numbers": sum(len(r["invented_numbers"]) for r in usable),
            "false_action_claims": sum(len(r["false_action_claims"]) for r in usable),
            "sanitizer_interventions": sum(1 for r in usable if r["sanitizer_intervened"]),
            "mean_swedish_ratio": round(statistics.mean([r["swedish_ratio"] for r in usable]), 3) if usable else None,
            "mean_coherence_proxy": round(statistics.mean([r["coherence_proxy"] for r in usable]), 3) if usable else None,
            "empty_answers": sum(1 for a in answers if not a),
        },
    }


def verdict(summary: dict) -> str:
    """PASS only when it is grounded AND did not bluff. Fluency alone never passes."""
    rate = summary.get("case_pass_rate")
    if summary["answered"] == 0:
        return "NO_ANSWER"
    if summary["false_action_claims"] or summary["invented_numbers"]:
        return "FAIL_UNGROUNDED"
    if rate is None or rate < 0.8:
        return "FAIL_LOW_COVERAGE"
    if (summary.get("mean_coherence_proxy") or 0) < 0.45 or (summary.get("mean_swedish_ratio") or 0) < 0.12:
        return "FAIL_LOW_COHERENCE"
    return "PASS"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args()

    settings = get_settings()
    db = SessionLocal()
    try:
        founder = _founder(db)
        facts = _facts(db, founder.id)
        context = format_project_status_instructions(db, owner_id=founder.id)
    finally:
        db.close()

    system = SYSTEM_PROMPT + "\n\n" + context
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime": settings.ollama_base_url,
        "questions": [q["question"] for q in QUESTIONS],
        "context_chars": len(context),
        "models": [],
    }
    for model in args.models:
        print(f"== {model} ==", flush=True)
        entry = run_model(
            base_url=settings.ollama_base_url,
            model=model,
            system=system,
            context=context,
            facts=facts,
            timeout=args.timeout,
        )
        entry["verdict"] = verdict(entry["summary"])
        print(json.dumps(entry["summary"], ensure_ascii=False), entry["verdict"], flush=True)
        report["models"].append(entry)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
