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
    """Raised by require_capability() for anything not currently available. Callers must treat
    this as a hard failure (see app/rag/mainai_jobs_service.py's create_job, which maps it
    straight to MainAIJobErrorCategory.capability_unavailable) — never silently degrade to a
    partial or best-effort execution.

    `reason` distinguishes WHY (founder re-review round, PR #36 — a plain "not available" was
    conflating two very different situations): `not_implemented` means this job_type doesn't
    exist in CAPABILITY_MANIFEST at all — a code-level fact, never becomes true just because a
    provider key is missing. `not_configured` means the capability IS implemented but nothing
    is currently wired up to actually execute it (e.g. no chat provider configured) — a
    founder-fixable runtime state, not a code gap. Routers/UIs can use this to show
    "not built yet" vs. "configure a provider first" instead of one indistinguishable 409."""

    def __init__(self, capability: str, *, reason: str = "not_implemented"):
        self.capability = capability
        self.reason = reason
        if reason == "not_configured":
            super().__init__(f"Capability '{capability}' is implemented but not currently configured to run.")
        else:
            super().__init__(f"Capability '{capability}' is not available.")


# The founder's explicit, reviewed list of what MainAI's runtime may currently claim to be
# able to execute as a durable job. Adding a new job_type without adding it here is a bug,
# not an oversight this module can auto-discover — see docs/MAINAI_JOB_RUNTIME.md's
# "capability manifest" section for how a real new capability gets added.
CAPABILITY_MANIFEST: frozenset[str] = frozenset({"corpus_review"})

# Which provider ROLE (see app/providers/registry.py's resolve_active) each capability's
# actual execution depends on being configured. corpus_review calls chat_with_fallback(), i.e.
# the "chat" role — a future capability with a different dependency gets its own entry here,
# never inherits this one by default (fail-closed: an unmapped capability is treated as
# unconfigured, see get_capability_status below).
_CAPABILITY_PROVIDER_ROLE: dict[str, str] = {"corpus_review": "chat"}

# corpus_review only ever reads existing documents/document_chunks and writes NEW
# mainai_job_proposals rows — it never modifies or deletes a founder's existing knowledge
# (documents/knowledge_claims/memory_source_units). Recorded here, not inferred, so a future
# capability that DOES modify existing data has to make that an explicit, reviewed choice
# instead of silently inheriting corpus_review's read-mostly shape.
_CAPABILITY_WRITE_PROFILE: dict[str, dict] = {
    "corpus_review": {"modifies_existing_data": False, "writes_new_records": True, "sandbox_only": False, "production_prohibited": False},
}


class CapabilityStatus(BaseModel):
    """The real, current, runtime-checked answer to "can MainAI actually do X right now" —
    replacing a bare "is this job_type on a hardcoded list" check (founder re-review round, PR
    #36: the old check said a capability was available even with zero AI providers configured,
    only failing later when the worker actually tried — conflating "implemented in code" with
    "executable right now"). No live network probe is made here (deliberately — see
    require_capability's docstring): `configured` reflects `provider.is_configured()`, the
    same cheap, no-network-call check app/providers/registry.py's resolve_chat_chain() already
    uses to build its fallback chain, not a real API round-trip."""

    capability: str
    implemented: bool
    configured: bool
    currently_available: bool
    modifies_existing_data: bool | None = None
    writes_new_records: bool | None = None
    sandbox_only: bool | None = None
    production_prohibited: bool | None = None
    requires_user_action: bool
    unavailable_reason: str | None = None


def get_capability_status(db, capability: str) -> CapabilityStatus:
    """Builds the real CapabilityStatus for `capability` against the CURRENT runtime state of
    `db`'s session. Fail-closed for anything not in CAPABILITY_MANIFEST OR not in
    _CAPABILITY_PROVIDER_ROLE/_CAPABILITY_WRITE_PROFILE (an implemented capability with no
    write-profile entry is a bug in this module, not a reason to guess a permissive default)."""
    implemented = capability in CAPABILITY_MANIFEST
    if not implemented:
        return CapabilityStatus(
            capability=capability,
            implemented=False,
            configured=False,
            currently_available=False,
            requires_user_action=False,
            unavailable_reason="not_implemented",
        )

    from app.providers.registry import resolve_active  # local import: this module stays DB/provider-free at import time

    role = _CAPABILITY_PROVIDER_ROLE.get(capability)
    configured = False
    if role is not None:
        try:
            provider, _model = resolve_active(db, role=role)
            configured = provider.is_configured()
        except Exception:  # noqa: BLE001 - resolve_active/is_configured failing means "not configured", never a 500 here
            configured = False

    profile = _CAPABILITY_WRITE_PROFILE.get(capability, {})
    currently_available = implemented and configured
    return CapabilityStatus(
        capability=capability,
        implemented=True,
        configured=configured,
        currently_available=currently_available,
        modifies_existing_data=profile.get("modifies_existing_data"),
        writes_new_records=profile.get("writes_new_records"),
        sandbox_only=profile.get("sandbox_only"),
        production_prohibited=profile.get("production_prohibited"),
        requires_user_action=not configured,
        unavailable_reason=None if currently_available else "not_configured",
    )


def require_capability(db, capability: str) -> None:
    """Fail closed: raises CapabilityUnavailableError unless `capability` is both implemented
    AND currently configured to actually run. Called BEFORE any MainAIJob row is created (see
    app/rag/mainai_jobs_service.py's create_job) — an unavailable capability must never reach
    the point of creating a job row at all, let alone claiming execution started. `db` is
    required (a behavior change from this function's pre-review-round signature, which took no
    db and could only ever check the static implemented-or-not list) — every call site already
    has a session in scope."""
    status = get_capability_status(db, capability)
    if not status.currently_available:
        raise CapabilityUnavailableError(capability, reason=status.unavailable_reason or "not_implemented")
