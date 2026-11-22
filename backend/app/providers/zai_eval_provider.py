"""Round 11 Phase 2: the ZAI provider adapter — an APPROVED MODEL available in this
engineering environment, reached through the `z-ai` CLI, behind the platform's standard
LLMProvider interface. This is NOT a production provider and registers nowhere by
default: it exists so the Cognitive Regression Gate's MODEL-MODE cases can evaluate real
model behavior through the real production call paths in THIS sandbox.

Design contract:
  - chat(): shells out to `z-ai chat` (system + user prompt via files to avoid argv
    length/quoting issues), parses the JSON output. No API keys are read, printed, or
    stored by this module — the CLI manages its own credentials.
  - embed(): DETERMINISTIC bag-of-words vector (documented as an EVALUATION-ONLY
    embedding). The z-ai CLI does not expose embeddings; the gate's model-mode cases do
    not measure retrieval quality (deterministic cases cover that) — they measure what
    the model DOES with the evidence the production prompt carries. A deterministic
    embedding keeps the corpus-to-prompt mapping stable across trials.
  - Failures raise ProviderError with a fixed classification ("unreachable") — never raw
    CLI output, which could contain arbitrary text.
"""

from __future__ import annotations

import json
import os
import hashlib
import math
import subprocess
import tempfile
from pathlib import Path

from app.config import get_settings
from app.providers.base import ChatResult, LLMProvider, Message, ProviderError

_DIM = get_settings().embedding_dim


class ZAIEvaluationProvider(LLMProvider):
    """Approved-model adapter for evaluation runs (see module docstring)."""

    name = "zai_eval"

    def _require_context(self):
        from app.providers.registry import _current_evaluation
        lease = _current_evaluation()
        if lease is None or lease.provider_class is not type(self):
            raise ProviderError("Evaluation provider requires an active authorized scope.",
                                category="unsupported", provider_request_may_have_left=False)
        return lease

    def is_configured(self) -> bool:
        return Path("/usr/local/bin/z-ai").exists() or _which_zai() is not None

    async def chat(self, messages: list[Message], model: str, *, timeout: float | None = None, **kwargs) -> ChatResult:
        lease = self._require_context()
        system_text = "\n\n".join(m.content for m in messages if m.role == "system")
        user_text = "\n\n".join(m.content for m in messages if m.role != "system")
        with tempfile.TemporaryDirectory(prefix="zai-gate-") as tmp:
            out_path = Path(tmp) / "out.json"
            # argv-form (list args, no shell): safe for arbitrary text; Linux argv limit
            # is ~2MB, far above any assembled prompt this gate produces.
            cmd = ["z-ai", "chat", "-p", user_text, "-s", system_text, "-o", str(out_path)]
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout or 180,
                    # CLI authentication must be independently provisioned; never inherit
                    # production HTTP provider keys, database credentials or app secrets.
                    env={key: os.environ[key] for key in
                         ("PATH","HOME","LANG","LC_ALL","LC_CTYPE","TMPDIR","XDG_CONFIG_HOME") if key in os.environ},
                )
            except FileNotFoundError as exc:
                raise ProviderError("z-ai CLI not available", category="unreachable", provider_request_may_have_left=False) from exc
            except subprocess.TimeoutExpired as exc:
                raise ProviderError("z-ai CLI timed out", category="unreachable", provider_request_may_have_left=True) from exc
            if proc.returncode != 0 or not out_path.exists():
                # Classify honestly: a 429 from the model service is RATE_LIMITED (a
                # quota condition, retryable later), not "unreachable".
                stderr = (proc.stderr or "") + (proc.stdout or "")
                if "429" in stderr or "Too many requests" in stderr:
                    raise ProviderError("Model service quota exhausted.", category="rate_limited", provider_request_may_have_left=True)
                raise ProviderError("z-ai CLI failed.", category="unreachable", provider_request_may_have_left=True)
            try:
                payload = json.loads(out_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ProviderError("z-ai CLI returned invalid output.", category="unreachable", provider_request_may_have_left=True) from exc
            # The CLI emits OpenAI-shaped JSON: choices[0].message.content + usage.
            try:
                content = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ProviderError("z-ai CLI output missing reply content.", category="unreachable", provider_request_may_have_left=True) from exc
            if not content or not str(content).strip():
                raise ProviderError("z-ai CLI returned an empty reply.", category="unreachable", provider_request_may_have_left=True)
            usage = payload.get("usage") or {}
            reported_model = payload.get("model")
            if (not isinstance(reported_model, str) or not reported_model.strip() or not isinstance(usage, dict)
                    or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("prompt_tokens","completion_tokens"))):
                raise ProviderError("z-ai CLI identity or usage is unverified.", category="unreachable",
                                    provider_request_may_have_left=True)
            lease.observation.update(system=system_text, requested_model=model, model=reported_model)
            return ChatResult(
                content=str(content), provider=self.name, model=reported_model,
                raw_usage={"prompt_tokens": usage.get("prompt_tokens", 0), "completion_tokens": usage.get("completion_tokens", 0)},
            )

    async def embed(self, texts: list[str], model: str, *, timeout: float | None = None) -> list[list[float]]:
        self._require_context()
        return [_bow_vec(t) for t in texts]


def _bow_vec(text: str) -> list[float]:
    v = [0.0] * _DIM
    for w in (text or "").lower().split():
        v[int.from_bytes(hashlib.sha256(w.encode()).digest()[:8], "big") % _DIM] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def _which_zai() -> str | None:
    for d in ("/usr/local/bin", "/usr/bin", "/home/z/.local/bin"):
        p = Path(d) / "z-ai"
        if p.exists():
            return str(p)
    return None


def evaluation_observation() -> dict:
    from app.providers.registry import _current_evaluation
    lease = _current_evaluation()
    return dict(lease.observation) if lease is not None else {}
