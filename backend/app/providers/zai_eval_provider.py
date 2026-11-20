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
import hashlib
import math
import subprocess
import tempfile
from pathlib import Path

from app.config import get_settings
from app.providers.base import ChatResult, LLMProvider, Message, ProviderError

_MODEL = "zai-cli"
_DIM = get_settings().embedding_dim


class ZAIEvaluationProvider(LLMProvider):
    """Approved-model adapter for evaluation runs (see module docstring)."""

    name = "zai_eval"
    last_system: str | None = None  # for the gate runner's prompt-level assertions
    last_model: str | None = None  # the model the CLI reports (e.g. glm-4-plus)

    def _require_context(self):
        from app.providers.registry import _current_evaluation
        lease = _current_evaluation()
        if lease is None or lease.provider_class is not type(self):
            raise ProviderError("Evaluation provider requires an active authorized scope.",
                                category="unsupported", provider_request_may_have_left=False)

    def is_configured(self) -> bool:
        return Path("/usr/local/bin/z-ai").exists() or _which_zai() is not None

    async def chat(self, messages: list[Message], model: str, *, timeout: float | None = None, **kwargs) -> ChatResult:
        self._require_context()
        system_text = "\n\n".join(m.content for m in messages if m.role == "system")
        user_text = "\n\n".join(m.content for m in messages if m.role != "system")
        with tempfile.TemporaryDirectory(prefix="zai-gate-") as tmp:
            out_path = Path(tmp) / "out.json"
            # argv-form (list args, no shell): safe for arbitrary text; Linux argv limit
            # is ~2MB, far above any assembled prompt this gate produces.
            cmd = ["z-ai", "chat", "-p", user_text, "-s", system_text, "-o", str(out_path)]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout or 180)
            except FileNotFoundError as exc:
                raise ProviderError("unreachable: z-ai CLI not available") from exc
            except subprocess.TimeoutExpired as exc:
                raise ProviderError("unreachable: z-ai CLI timed out") from exc
            if proc.returncode != 0 or not out_path.exists():
                # Classify honestly: a 429 from the model service is RATE_LIMITED (a
                # quota condition, retryable later), not "unreachable".
                stderr = (proc.stderr or "") + (proc.stdout or "")
                if "429" in stderr or "Too many requests" in stderr:
                    raise ProviderError("rate_limited: model service quota exhausted — retry later")
                raise ProviderError("unreachable: z-ai CLI failed (exit %s)" % proc.returncode)
            try:
                payload = json.loads(out_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ProviderError("unreachable: z-ai CLI returned unparseable output") from exc
            # The CLI emits OpenAI-shaped JSON: choices[0].message.content + usage.
            try:
                content = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ProviderError("unreachable: z-ai CLI output missing reply content") from exc
            if not content or not str(content).strip():
                raise ProviderError("unreachable: z-ai CLI returned an empty reply")
            usage = payload.get("usage") or {}
            reported_model = payload.get("model") or (model or _MODEL)
            ZAIEvaluationProvider.last_system = system_text
            ZAIEvaluationProvider.last_model = reported_model
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
