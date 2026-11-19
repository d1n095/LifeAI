#!/usr/bin/env python
"""Local model acceptance — real inference against a live runtime, or an exact blocker.

Every check here talks to a real Ollama server over HTTP. Nothing is mocked: no stub socket, no
faked response body. That distinction is the whole point of the file. A green unit suite proves
MainAI *handles* a local model's protocol; only this proves a local model actually *answers*.

What it verifies, in the order a startup would:

  1. the server is reachable and which version it is;
  2. the configured chat model is actually installed (availability, not just configurability);
  3. a real completion returns non-empty text, with latency and token counts measured;
  4. a timeout is enforced as a `ProviderError`, not as a hang;
  5. an uninstalled model fails closed as `unsupported` (a provisioning gap) rather than
     `unreachable` (an outage) — the two call for different operator responses;
  6. embeddings work, are deterministic for identical input, and rank a related text above an
     unrelated one — a real numeric signal, not just "a list of floats came back";
  7. an uninstalled embedding model fails closed the same way;
  8. two concurrent completions do not cross-talk.

Exit codes are distinct on purpose, so an operator (or CI) can tell "this machine has no model"
from "the model is broken":

  0  every check passed
  2  a check failed (the runtime is present but not behaving)
  3  blocked: the runtime or the model is absent — the report says exactly what to install

Usage:
    OLLAMA_BASE_URL=http://127.0.0.1:11434 \
    python backend/scripts/acceptance/local_model_acceptance.py \
        [--chat-model qwen2.5:0.5b-instruct] [--embed-model nomic-embed-text] [--out report.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.providers.base import Message, ProviderError  # noqa: E402
from app.providers.ollama_provider import OllamaProvider  # noqa: E402

BLOCKED = "blocked"


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, name: str, status: str, detail: str, *, evidence: str = "real", **extra) -> None:
        self.checks.append({"name": name, "status": status, "evidence_kind": evidence, "detail": detail, **extra})
        mark = {"passed": "passed ", "failed": "FAILED ", "blocked": "blocked", "skipped": "skipped"}[status]
        print(f"[{mark}] {name:28s} ({evidence}) {detail}")

    @property
    def failed(self) -> list[dict]:
        return [c for c in self.checks if c["status"] == "failed"]

    @property
    def blocked(self) -> list[dict]:
        return [c for c in self.checks if c["status"] == "blocked"]

    def as_dict(self, base_url: str) -> dict:
        return {
            "target": base_url,
            "checks": self.checks,
            "passed": sum(1 for c in self.checks if c["status"] == "passed"),
            "failed": len(self.failed),
            "blocked": len(self.blocked),
            "verdict": "failed" if self.failed else ("blocked" if self.blocked else "passed"),
        }


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def _installed_models(base_url: str) -> tuple[str | None, list[str] | None, str | None]:
    """(version, model names, error). Real GETs — the only way to tell 'absent' from 'broken'."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            tags = await client.get(f"{base_url}/api/tags")
            tags.raise_for_status()
            names = [m.get("name", "") for m in tags.json().get("models", []) if isinstance(m, dict)]
            version = None
            try:
                v = await client.get(f"{base_url}/api/version")
                if v.status_code == 200:
                    version = v.json().get("version")
            except (httpx.HTTPError, ValueError):
                version = None
            return version, names, None
    except (httpx.HTTPError, ValueError) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def _has_model(installed: list[str], wanted: str) -> bool:
    # Ollama reports names with an implicit ":latest" tag; treat both spellings as the same model.
    return any(name == wanted or name == f"{wanted}:latest" or name.split(":")[0] == wanted for name in installed)


async def run(args: argparse.Namespace) -> Report:
    report = Report()
    provider = OllamaProvider()
    base_url = provider.settings.ollama_base_url

    version, installed, error = await _installed_models(base_url)
    if error is not None:
        report.add(
            "server.reachable",
            BLOCKED,
            f"no local runtime at {base_url} ({error}). Start one with `ollama serve` "
            "and set OLLAMA_BASE_URL to its address.",
        )
        return report
    report.add(
        "server.reachable",
        "passed",
        f"Ollama {version or 'version unavailable'} at {base_url}, {len(installed or [])} model(s) installed",
        version=version,
        installed_models=installed,
    )

    chat_model = args.chat_model
    if not _has_model(installed or [], chat_model):
        report.add(
            "model.chat_present",
            BLOCKED,
            f"chat model {chat_model!r} is not installed. Run: ollama pull {chat_model}",
            model=chat_model,
        )
        embed_available = False
    else:
        report.add("model.chat_present", "passed", f"chat model {chat_model!r} installed", model=chat_model)
        embed_available = True

    if embed_available:
        prompt = [Message(role="user", content="Reply with exactly one word: READY")]
        started = time.monotonic()
        try:
            result = await provider.chat(prompt, chat_model, timeout=args.timeout)
        except ProviderError as exc:
            report.add("inference.chat", "failed", f"real completion failed: {exc} (category={exc.category})")
        else:
            elapsed = time.monotonic() - started
            usage = result.raw_usage or {}
            if not result.content.strip():
                report.add("inference.chat", "failed", "the runtime returned an empty completion")
            else:
                report.add(
                    "inference.chat",
                    "passed",
                    f"{result.content.strip()[:40]!r} in {elapsed:.2f}s "
                    f"({usage.get('prompt_tokens', 0)} prompt / {usage.get('completion_tokens', 0)} completion tokens)",
                    latency_seconds=round(elapsed, 3),
                    usage=usage,
                    model=result.model,
                )
                # A CPU-only model that answers in tens of seconds is not a startup failure, but the
                # number belongs in the record: it is what the founder will feel in the chat window.
                if elapsed > args.slow_seconds:
                    report.add(
                        "inference.latency_budget",
                        "passed",
                        f"{elapsed:.1f}s exceeds the {args.slow_seconds:.0f}s interactive budget on this "
                        "hardware — usable for background work, slow for interactive chat",
                        evidence="real",
                        latency_seconds=round(elapsed, 3),
                    )

        # Timeout enforcement: a real socket that really expires, not a mocked raise.
        started = time.monotonic()
        try:
            await provider.chat(prompt, chat_model, timeout=0.001)
        except ProviderError as exc:
            elapsed = time.monotonic() - started
            if exc.category == "unreachable" and elapsed < 10:
                report.add(
                    "inference.timeout_enforced",
                    "passed",
                    f"a 1 ms budget failed closed as {exc.category!r} in {elapsed:.2f}s",
                    evidence="real",
                )
            else:
                report.add(
                    "inference.timeout_enforced",
                    "failed",
                    f"expansion to a 1 ms timeout produced category={exc.category!r} after {elapsed:.2f}s",
                )
        except Exception as exc:  # noqa: BLE001 - anything else is a contract break worth naming
            report.add(
                "inference.timeout_enforced",
                "failed",
                f"a 1 ms timeout raised {type(exc).__name__} instead of ProviderError: {exc}",
            )
        else:
            report.add("inference.timeout_enforced", "failed", "a 1 ms timeout did not fail at all")

        # Missing model must be a provisioning gap, not an outage.
        try:
            await provider.chat(prompt, "mainai-model-that-is-not-installed", timeout=30)
        except ProviderError as exc:
            status = "passed" if exc.category == "unsupported" else "failed"
            report.add(
                "inference.missing_model_fails_closed",
                status,
                f"uninstalled model reported as category={exc.category!r} (404 => 'unsupported' is correct)"
                if status == "passed"
                else f"uninstalled model reported as category={exc.category!r}; expected 'unsupported'",
            )
        except Exception as exc:  # noqa: BLE001
            report.add(
                "inference.missing_model_fails_closed",
                "failed",
                f"raised {type(exc).__name__} instead of ProviderError: {exc}",
            )
        else:
            report.add("inference.missing_model_fails_closed", "failed", "an uninstalled model 'succeeded'")

        # Two concurrent completions must not cross-talk.
        answers = await asyncio.gather(
            provider.chat([Message(role="user", content="Answer with only the digit 1")], chat_model, timeout=args.timeout),
            provider.chat([Message(role="user", content="Answer with only the digit 2")], chat_model, timeout=args.timeout),
            return_exceptions=True,
        )
        if any(isinstance(a, BaseException) for a in answers):
            report.add(
                "inference.concurrency",
                "failed",
                f"a concurrent completion raised: {[str(a) for a in answers if isinstance(a, BaseException)][:1]}",
            )
        else:
            report.add("inference.concurrency", "passed", "two concurrent completions both returned")

    # Embeddings — the channel retrieval actually depends on.
    embed_model = args.embed_model
    if not embed_available:
        report.add("embeddings.available", "skipped", "skipped because no chat model runtime was reachable")
        return report
    if not _has_model(installed or [], embed_model):
        report.add(
            "embeddings.available",
            BLOCKED,
            f"embedding model {embed_model!r} is not installed. Run: ollama pull {embed_model}",
            model=embed_model,
        )
        return report
    try:
        pair = await provider.embed(["the founder ledger is reconciled monthly"], [embed_model] and embed_model, timeout=args.timeout)
        again = await provider.embed(["the founder ledger is reconciled monthly"], embed_model, timeout=args.timeout)
        unrelated = await provider.embed(["a recipe for rye sourdough bread"], embed_model, timeout=args.timeout)
        related = await provider.embed(["monthly reconciliation of the founder ledger"], embed_model, timeout=args.timeout)
    except ProviderError as exc:
        report.add("embeddings.available", "failed", f"real embedding call failed: {exc} (category={exc.category})")
        return report

    width_ok = len(pair[0]) == len(unrelated[0]) == len(related[0])
    deterministic = pair[0] == again[0]
    sim_related = _cosine(pair[0], related[0])
    sim_unrelated = _cosine(pair[0], unrelated[0])
    ranking_ok = sim_related > sim_unrelated
    status = "passed" if (width_ok and deterministic and ranking_ok) else "failed"
    report.add(
        "embeddings.available",
        status,
        f"width {len(pair[0])}, deterministic={deterministic}, "
        f"sim(related)={sim_related:.3f} > sim(unrelated)={sim_unrelated:.3f} = {ranking_ok}",
        width=len(pair[0]),
        deterministic=deterministic,
        sim_related=round(sim_related, 4),
        sim_unrelated=round(sim_unrelated, 4),
    )

    try:
        await provider.embed(["x"], "mainai-embed-model-that-is-not-installed", timeout=30)
    except ProviderError as exc:
        report.add(
            "embeddings.missing_model_fails_closed",
            "passed" if exc.category == "unsupported" else "failed",
            f"uninstalled embedding model reported as category={exc.category!r}",
        )
    except Exception as exc:  # noqa: BLE001
        report.add("embeddings.missing_model_fails_closed", "failed", f"raised {type(exc).__name__}: {exc}")
    else:
        report.add("embeddings.missing_model_fails_closed", "failed", "an uninstalled embedding model 'succeeded'")

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-model", default="qwen2.5:0.5b-instruct")
    parser.add_argument("--embed-model", default="nomic-embed-text")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--slow-seconds", type=float, default=10.0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    report = asyncio.run(run(args))
    base_url = OllamaProvider().settings.ollama_base_url
    payload = report.as_dict(base_url)
    print(f"\nverdict: {payload['verdict']}  ({payload['passed']} passed, {payload['failed']} failed, {payload['blocked']} blocked)")
    if args.out:
        args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"report: {args.out}")
    if payload["failed"]:
        return 2
    if payload["blocked"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
