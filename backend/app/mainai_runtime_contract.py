"""MainAI Runtime Truthfulness contract — the founder's core requirement for this branch:
MainAI must never be able to claim it "started" or "is working" on something unless a real,
persisted `MainAIJob` row backs that claim, and every execution-shaped response must carry
that job's real id so a human (or a test) can independently verify it.

This module owns two things:
  1. `ExecutionResponseMode` — the closed set of response shapes MainAI's runtime is allowed
     to produce, and `MainAIExecutionResponse`, a Pydantic model whose OWN validator makes it
     structurally impossible to construct an `execution_started`/`status`/`completed`/
     `failed`/`cancelled` response without a real `job_id` — this is enforced at
     object-construction time, not by convention or code review.
  2. `CAPABILITY_MANIFEST` + `require_capability()` — a fixed, reviewed list of capabilities
     MainAI's runtime may currently claim to have. `require_capability()` raises
     `CapabilityUnavailableError` (mapped to `MainAIJobErrorCategory.capability_unavailable`
     by app/rag/mainai_jobs_service.py) for anything not on the list — "fail closed when the
     requested capability is unavailable" from the founder's own spec, not a soft warning.

Nothing in this module talks to the database — it is a pure contract layer, deliberately
small and easy to audit on its own, imported by app/rag/mainai_jobs_service.py (which DOES
talk to the database) and by app/routers/mainai_jobs.py (which builds the actual HTTP
responses). Kept separate from app/schemas.py's per-domain Pydantic blocks because this one
encodes a cross-cutting SAFETY RULE, not a single endpoint's request/response shape.
"""

import enum
import uuid

from pydantic import BaseModel, model_validator


class ExecutionResponseMode(str, enum.Enum):
    """What kind of thing MainAI is telling the caller. `answer`/`proposal` describe MainAI
    reasoning about something WITHOUT claiming to execute anything durable (e.g. a plain
    chat reply, or "here is what I would propose") — no job_id is required or expected for
    either. Every other mode describes a real, persisted job and MUST carry its id."""

    answer = "answer"
    proposal = "proposal"
    execution_started = "execution_started"
    status = "status"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


# Modes that describe a durable job and therefore require a real job_id.
_JOB_BACKED_MODES = frozenset(
    {
        ExecutionResponseMode.execution_started,
        ExecutionResponseMode.status,
        ExecutionResponseMode.completed,
        ExecutionResponseMode.failed,
        ExecutionResponseMode.cancelled,
    }
)


class MainAIExecutionResponse(BaseModel):
    """The one shape every MainAI runtime response must fit. `job_id` is `None` for
    `answer`/`proposal` and REQUIRED (validated below, not just typed as optional) for every
    job-backed mode — there is no way to construct e.g. an `execution_started` response
    without a real, already-persisted job id, which is the founder's literal requirement:
    'every execution response must include the real job ID'. `message` is always the
    already-safe, bounded public text (see MainAIJob.public_message) — never raw internal
    detail."""

    mode: ExecutionResponseMode
    job_id: uuid.UUID | None = None
    message: str

    @model_validator(mode="after")
    def _job_id_required_for_job_backed_modes(self) -> "MainAIExecutionResponse":
        if self.mode in _JOB_BACKED_MODES and self.job_id is None:
            raise ValueError(f"job_id is required for response mode '{self.mode.value}' — no phantom job claims.")
        if self.mode not in _JOB_BACKED_MODES and self.job_id is not None:
            raise ValueError(f"job_id must be None for response mode '{self.mode.value}' — it does not describe a durable job.")
        return self


class CapabilityUnavailableError(Exception):
    """Raised by require_capability() for anything not on CAPABILITY_MANIFEST. Callers must
    treat this as a hard failure (see app/rag/mainai_jobs_service.py's create_job, which maps
    it straight to MainAIJobErrorCategory.capability_unavailable) — never silently degrade to
    a partial or best-effort execution."""

    def __init__(self, capability: str):
        self.capability = capability
        super().__init__(f"Capability '{capability}' is not available.")


# The founder's explicit, reviewed list of what MainAI's runtime may currently claim to be
# able to execute as a durable job. Adding a new job_type without adding it here is a bug,
# not an oversight this module can auto-discover — see docs/MAINAI_JOB_RUNTIME.md's
# "capability manifest" section for how a real new capability gets added.
CAPABILITY_MANIFEST: frozenset[str] = frozenset({"corpus_review"})


def require_capability(capability: str) -> None:
    """Fail closed: raises CapabilityUnavailableError for anything not in
    CAPABILITY_MANIFEST. Called BEFORE any MainAIJob row is created (see
    app/rag/mainai_jobs_service.py's create_job) — an unavailable capability must never reach
    the point of creating a job row at all, let alone claiming execution started."""
    if capability not in CAPABILITY_MANIFEST:
        raise CapabilityUnavailableError(capability)
