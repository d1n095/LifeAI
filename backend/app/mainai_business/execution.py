"""Worker execution strategies for the business engine.

Two interchangeable runners implement the same contract, so the engine (and every security
guarantee around it) is identical whichever is used:

- `InProcessRunner` — deterministic local computation (the #335 default; no provider).
- `ModelBackedRunner` — REAL local-model inference via the existing provider abstraction
  (#334). Fails closed if the resolved chat provider is not local (no paid fallback), and
  fails the task — never fabricates a result — if the model output does not parse into the
  capability's schema.

Both read the worker's inputs from the ASSIGNMENT'S CONTEXT PACKAGE only, so a worker can
never see anything outside the context the broker was authorised to disclose to it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.mainai_business import workers as inprocess_workers
from app.mainai_business.prompts import build_messages
from app.mainai_business.schemas import SchemaValidationError, parse_capability_output
from app.models.workforce import WorkforceAssignment, WorkforceContextPackage


class ModelBackedExecutionError(Exception):
    pass


@dataclass
class ExecutionResult:
    ok: bool
    data: dict | None
    receipt: dict = field(default_factory=dict)
    validator_ref: str | None = None
    failure_reason: str | None = None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run_async(coro):
    """Run an async provider call from the engine's synchronous context."""
    return asyncio.run(coro)


def inputs_from_package(pkg: WorkforceContextPackage | None) -> dict[str, dict]:
    """Reconstruct upstream findings from the permitted context package ONLY."""
    inputs: dict[str, dict] = {}
    for item in (pkg.items if pkg else []) or []:
        ref = str(item.get("ref") or "")
        if item.get("kind") == "excerpt" and ref.startswith("workflow:"):
            dep = ref.rsplit(":", 1)[-1]
            excerpt = item.get("excerpt")
            if isinstance(excerpt, dict):
                inputs[dep] = excerpt
    return inputs


class InProcessRunner:
    mode = "in_process"
    correction_attempts = 0  # deterministic — no validator-feedback retries

    def run(
        self,
        db: Session,
        *,
        owner_id,
        assignment: WorkforceAssignment,
        task,
        objective_text: str,
        feedback: str | None = None,  # ignored; deterministic output
    ) -> ExecutionResult:
        pkg = db.get(WorkforceContextPackage, assignment.context_package_id)
        inputs = inputs_from_package(pkg)
        data = inprocess_workers.run_worker(task.capability, objective_text=objective_text, inputs=inputs)
        return ExecutionResult(
            ok=True,
            data=data,
            receipt={"runtime": "in_process_deterministic", "provider_invoked": False},
            validator_ref=None,
        )


class ModelBackedRunner:
    mode = "model_backed"

    def __init__(
        self,
        provider=None,
        model: str | None = None,
        *,
        timeout: float = 120.0,
        require_local: bool = True,
        attempts: int = 1,
        correction_attempts: int = 0,
    ):
        # provider/model may be injected (tests); otherwise resolved from the DB config / env.
        self._provider = provider
        self._model = model
        self._timeout = timeout
        self._require_local = require_local
        # Small local models occasionally emit unparseable text; retrying the SAME task a few
        # times is legitimate (it never bypasses verification). Default 1 keeps tests exact.
        self._attempts = max(1, int(attempts))
        # Bounded, authorized validator-feedback correction attempts (engine-driven). 0 keeps
        # tests exact. Read by the engine via the `correction_attempts` attribute.
        self.correction_attempts = max(0, int(correction_attempts))

    def _resolve(self, db: Session):
        if self._provider is not None:
            return self._provider, (self._model or "injected")
        from app.providers.registry import resolve_active

        provider, model = resolve_active(db, role="chat")
        if self._require_local and getattr(provider, "name", None) != "ollama":
            # Fail closed: model-backed specialists run on local inference only. Never a paid
            # provider, and never a silent fallback to one.
            raise ModelBackedExecutionError(
                f"refusing non-local chat provider {getattr(provider, 'name', '?')!r}: "
                "model-backed workers require local inference (no paid-provider fallback)"
            )
        return provider, model

    def run(
        self,
        db: Session,
        *,
        owner_id,
        assignment: WorkforceAssignment,
        task,
        objective_text: str,
        feedback: str | None = None,
    ) -> ExecutionResult:
        pkg = db.get(WorkforceContextPackage, assignment.context_package_id)
        inputs = inputs_from_package(pkg)
        provider, model = self._resolve(db)  # may raise ModelBackedExecutionError (fail closed)

        messages = build_messages(task.capability, objective_text=objective_text, inputs=inputs, feedback=feedback)
        prompt_repr = json.dumps([{"role": m.role, "content": m.content} for m in messages], sort_keys=True)
        receipt: dict = {
            "runtime": "model_backed",
            "provider": getattr(provider, "name", None),
            "model": model,
            "provider_invoked": True,
            "prompt_sha256": _sha(prompt_repr),
            "context_tasks": sorted(inputs.keys()),
        }

        last_parse_error: str | None = None
        t0 = time.monotonic()
        for attempt in range(1, self._attempts + 1):
            try:
                result = _run_async(provider.chat(messages, model=model, timeout=self._timeout))
            except Exception as exc:  # provider unreachable / timeout — report, never bypass
                receipt["latency_ms"] = int((time.monotonic() - t0) * 1000)
                receipt["error"] = f"{type(exc).__name__}: {exc}"
                receipt["attempts"] = attempt
                return ExecutionResult(ok=False, data=None, receipt=receipt, failure_reason="provider_error")

            receipt["raw_output_sha256"] = _sha(result.content or "")
            receipt["usage"] = dict(result.raw_usage or {})
            receipt["provider"] = result.provider  # what actually answered
            receipt["model"] = result.model
            try:
                data = parse_capability_output(task.capability, result.content)
            except SchemaValidationError as exc:
                last_parse_error = str(exc)
                continue  # retry the same task (does not bypass verification)

            receipt["latency_ms"] = int((time.monotonic() - t0) * 1000)
            receipt["parsed_ok"] = True
            receipt["attempts"] = attempt
            return ExecutionResult(
                ok=True,
                data=data,
                receipt=receipt,
                validator_ref=f"mainai_business.validators.{task.capability}",
            )

        receipt["latency_ms"] = int((time.monotonic() - t0) * 1000)
        receipt["parsed_ok"] = False
        receipt["parse_error"] = last_parse_error
        receipt["attempts"] = self._attempts
        return ExecutionResult(ok=False, data=None, receipt=receipt, failure_reason="schema_validation_failed")
