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
     by app/jobs/service.py) for anything not on the list — "fail closed when the
     requested capability is unavailable" from the founder's own spec, not a soft warning.

Nothing in this module talks to the database — it is a pure contract layer, deliberately
small and easy to audit on its own, imported by app/jobs/service.py (which DOES
talk to the database) and by app/routers/mainai_jobs.py (which builds the actual HTTP
responses). Kept separate from app/schemas.py's per-domain Pydantic blocks because this one
encodes a cross-cutting SAFETY RULE, not a single endpoint's request/response shape.
"""

import enum
import re
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


def build_answer_response(message: str) -> "MainAIExecutionResponse":
    """The one, structurally-enforced way to construct a plain informational reply — `mode=
    answer`, `job_id=None` always. Founder re-review round (PR #36): before this, nothing in
    the codebase actually constructed a MainAIExecutionResponse anywhere outside this module's
    own tests — the contract existed but no live surface used it, so the founder's original
    goal ("MainAI must never be able to claim it started/is working on something without a
    real job") remained completely unenforced on app/routers/chat.py, the highest-traffic
    surface in the system. This is now that live wiring's entry point (see chat.py's
    _attempt_assistant_reply): `message` is sanitized via sanitize_unverified_execution_claims
    below BEFORE this is ever called, so this function's own Pydantic validation (job_id must
    be None for `answer`) is a real, enforced guarantee about the object this call site
    produces, not just a shape nothing here ever exercises."""
    return MainAIExecutionResponse(mode=ExecutionResponseMode.answer, job_id=None, message=message)


# Founder re-review round (PR #36), explicit requirement: "the model must not be able to
# produce 'I'm working on it in the background', 'the job has started', or 'done' as a
# STRUCTURAL execution claim without contract proof." The PRIMARY defense is prompt-level (see
# chat.py's SYSTEM_PROMPT, which now explicitly tells the model it has no background-execution
# capability on this path and must never claim one) plus the structural job_id=None guarantee
# build_answer_response() above enforces. This pattern list is explicitly SECONDARY
# defense-in-depth for the one channel neither of those constrains — the model's own free-text
# content — not "the only protection."
#
# Fourth re-review round (same PR): an earlier version of this function only ever APPENDED a
# correction after the model's original text, so a message could (and, once tested end-to-end,
# demonstrably did) end up telling the founder "Jag arbetar med det i bakgrunden." immediately
# followed by "[MainAI-obs: detta svar beskriver inget verkligt bakgrundsjobb...]" — both claims
# visible in the same message at once. The founder's review classified that as HIGH: the user
# can still be left with self-contradictory, misleading text. This version REPLACES the specific
# sentence containing an unverified execution claim with a fixed truthful sentence instead of
# appending after it — the false claim itself is gone from what the founder actually reads, not
# just followed by a disclaimer.
#
# Still deliberately NOT a general sentiment/intent classifier (unreviewable, prone to false
# positives on unrelated text) and still explicitly not "the only protection" — it is defense in
# depth on top of (1) the system-prompt instruction and (2) build_answer_response()'s structural
# job_id=None guarantee, which is what actually prevents this response from ever being
# CLASSIFIED as execution_started/status/completed. This function only ever affects the free-text
# CONTENT, never the response's structural mode.
#
# Each rule below is a regex (not a bare substring) so it matches reasonable inflections of the
# same claim, not just one exact phrasing — the founder's explicit instruction to "avoid a
# growing open keyword list as the only solution." It remains a deterministic, reviewable ruleset
# rather than a second model call (which would add its own latency/cost/failure surface and
# would itself need review) — residual bypass risk for a genuinely novel phrasing this ruleset
# doesn't anticipate is real and is documented in docs/MAINAI_JOB_RUNTIME.md, not hidden. Seven
# claim categories, Swedish and English, matching the founder's explicit list:
#   1. working/laboring in the background
#   2. will get back to you / follow up later
#   3. monitoring/watching it
#   4. has already reviewed everything
#   5. is done/finished
#   6. has started the job
#   7. will notify you when done
_EXECUTION_CLAIM_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # 1. working/laboring in the background
    (re.compile(r"\b(jag\s+)?(arbetar|jobbar|kör|bearbetar|processar)\s+(med\s+|på\s+)?(det|detta|frågan|dokumenten)?\s*i\s+bakgrunden\b", re.I), "sv"),
    (re.compile(r"\b(i'?m|i\s+am)\s+working\s+on\s+(it|this|that)\s+in\s+the\s+background\b", re.I), "en"),
    (re.compile(r"\brunning\s+(this|it)\s+in\s+the\s+background\b", re.I), "en"),
    (re.compile(r"\bworking\s+on\s+(it|this)\s+in\s+the\s+background\b", re.I), "en"),
    # 2. will get back to you / follow up later
    (re.compile(r"\bjag\s+(återkommer|hör av mig|kommer att höra av mig)\s+(till dig\s+)?(senare|strax|snart|inom kort)\b", re.I), "sv"),
    (re.compile(r"\bi'?ll\s+(get back to you|follow up|update you|circle back)\s+(later|soon|shortly)\b", re.I), "en"),
    # 3. monitoring/watching it
    (re.compile(r"\bjag\s+(övervakar|bevakar|monitorerar|håller koll på)\s+(det|detta|situationen|läget)\b", re.I), "sv"),
    (re.compile(r"\bi'?m\s+(monitoring|keeping an eye on|watching)\s+(it|this|the situation)\b", re.I), "en"),
    # 4. has already reviewed everything
    (re.compile(r"\bjag\s+har\s+(granskat|gått igenom|läst igenom|kontrollerat)\s+(allt|alla|allting|hela)\b", re.I), "sv"),
    (re.compile(r"\bi'?ve\s+(reviewed|checked|gone through|looked at)\s+(everything|all of (it|them))\b", re.I), "en"),
    # 5. is done/finished -- deliberately requires an explicit job/task/work noun, NOT a bare
    # "jag är"/"det är"/"i'm"/"it's"/"this is" (those matched ordinary sentences with nothing to
    # do with a background job -- e.g. "Jag är klar med kaffet" / "It's done!" -- a real false
    # positive found and fixed during this round's own self-review; see
    # docs/MAINAI_JOB_RUNTIME.md).
    (
        re.compile(
            r"\b(jobbet|uppgiften|granskningen|körningen|arbetet|bakgrundsjobbet)\s+(är|har blivit)\s+"
            r"(klar|färdig|klart|färdigt|avslutad|avslutat|slutförd|slutfört)\b",
            re.I,
        ),
        "sv",
    ),
    (re.compile(r"\bjag\s+är\s+klar\s+med\s+(jobbet|uppgiften|granskningen|körningen|arbetet|det(ta)?\s+jobbet)\b", re.I), "sv"),
    (
        re.compile(
            r"\b(the\s+job|the\s+task|the\s+review|the\s+background\s+job|my\s+work\s+on\s+this)\s+is\s+"
            r"(done|finished|complete|completed)\b",
            re.I,
        ),
        "en",
    ),
    (re.compile(r"\bi'?m\s+done\s+with\s+(the\s+job|the\s+task|the\s+review|this\s+job)\b", re.I), "en"),
    # 6. has started the job
    (re.compile(r"\b(jobbet|uppgiften|körningen)\s+(har|är)\s+startat\b", re.I), "sv"),
    (re.compile(r"\bjag\s+har\s+(startat|påbörjat|initierat)\s+(jobbet|uppgiften|körningen)\b", re.I), "sv"),
    (re.compile(r"\bthe\s+(job|task)\s+(has\s+started|is\s+now\s+running)\b", re.I), "en"),
    (re.compile(r"\bi'?ve?\s+(started|initiated|kicked off)\s+the\s+(job|task)\b", re.I), "en"),
    # 7. will notify you when done
    (re.compile(r"\bjag\s+(kommer\s+att\s+)?(meddela|höra av mig|återkomma till dig)\s+när\s+(det|jobbet|granskningen|detta|uppgiften)\s+är\s+(klart|färdigt)\b", re.I), "sv"),
    (re.compile(r"\bi'?ll\s+(let you know|notify you|update you|ping you)\s+when\s+(it'?s?|this is|the job is|the task is)\s+(done|ready|finished|complete)\b", re.I), "en"),
)

_TRUTHFUL_REPLACEMENT: dict[str, str] = {
    "sv": "[MainAI: det här svaret är hela mitt arbete med frågan — inget bakgrundsjobb pågår eller är avslutat.]",
    "en": "[MainAI: this reply is the entire work on your question — no background job is running or has been completed.]",
}

# Sentence boundary: end-of-sentence punctuation followed by whitespace, or a newline. A
# deliberately simple heuristic (matches this codebase's other lightweight text-processing
# choices) — good enough to isolate a single offending sentence from the rest of an otherwise
# legitimate answer without needing a real NLP sentence tokenizer for this narrow purpose.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def sanitize_unverified_execution_claims(message: str) -> str:
    """Replaces (never just follows) every sentence containing an unverified execution claim
    with a fixed truthful sentence, leaving the rest of the message untouched. Founder
    re-review round (PR #36), fourth pass: the previous append-only version left the model's
    original false claim fully visible, immediately followed by a correction — a
    self-contradictory message the founder classified HIGH. This version removes the claim
    itself from what the founder reads.

    Naturally idempotent: the replacement sentences themselves never match
    `_EXECUTION_CLAIM_RULES`, so a second call on already-sanitized text is a no-op — no
    separate "already sanitized" check is needed the way the previous append-only notice
    required one. Safe to call unconditionally on every chat reply, matching chat.py's call
    site usage."""
    sentences = _SENTENCE_SPLIT_RE.split(message)
    output: list[str] = []
    last_was_replacement = False
    changed = False
    for sentence in sentences:
        matched_lang: str | None = None
        for pattern, lang in _EXECUTION_CLAIM_RULES:
            if pattern.search(sentence):
                matched_lang = lang
                break
        if matched_lang is not None:
            changed = True
            if not last_was_replacement:
                output.append(_TRUTHFUL_REPLACEMENT[matched_lang])
            last_was_replacement = True
        else:
            if sentence.strip():
                output.append(sentence)
            last_was_replacement = False
    if not changed:
        return message
    return " ".join(output).strip()


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
    this as a hard failure (see app/jobs/service.py's create_job, which maps it
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
CAPABILITY_MANIFEST: frozenset[str] = frozenset(
    {"corpus_review", "message_sequence_backfill", "structured_export_import", "task_execution"}
)

# Which provider ROLE (see app/providers/registry.py's resolve_active) each capability's
# actual execution depends on being configured. corpus_review calls chat_with_fallback(), i.e.
# the "chat" role — a future capability with a different dependency gets its own entry here,
# never inherits this one by default (fail-closed: a capability MISSING from this dict is
# treated as unconfigured, see get_capability_status below).
#
# An explicit `None` is NOT the same thing as a missing entry, and the difference is
# load-bearing: `None` means "this capability was reviewed and genuinely depends on no AI
# provider at all", which is the founder's standing "the system must keep working without AI
# wherever that's architecturally possible" rule made enforceable. `message_sequence_backfill`
# (S1B, app/rag/backfill/message_sequence.py) is pure SQL over rows that already exist —
# numbering the founder's own message history must not become unavailable because no model key
# happens to be configured. A capability simply forgotten from this dict still fails closed.
#
# `task_execution` (MainAI Execution Loop V0.1, app/mainai_execution/execution_job.py) depends
# on "chat" for its two AI-backed task_types (`repo_edit`'s code-agent call, `read_only_audit`'s
# analysis call) — `run_tests`/`open_pr` need no provider at all, but the capability-level
# check reports the typical/primary dependency, same convention corpus_review already sets;
# require_capability() is re-checked per-dispatch (app/worker.py) so an unconfigured provider
# is caught even for a task whose OWN task_type wouldn't have needed it.
_CAPABILITY_PROVIDER_ROLE: dict[str, str | None] = {
    "corpus_review": "chat",
    "message_sequence_backfill": None,
    # Format-agnostic structured import is deterministic local parsing. The production
    # adapter registry intentionally stays empty until a real export format is verified.
    "structured_export_import": None,
    "task_execution": "chat",
}

# corpus_review only ever reads existing documents/document_chunks and writes NEW
# mainai_job_proposals rows — it never modifies or deletes a founder's existing knowledge
# (documents/knowledge_claims/memory_source_units). Recorded here, not inferred, so a future
# capability that DOES modify existing data has to make that an explicit, reviewed choice
# instead of silently inheriting corpus_review's read-mostly shape.
#
# message_sequence_backfill is exactly such a capability and says so: it UPDATEs existing
# `messages` rows (NULL -> an ordinal). The modification is strictly additive in meaning and
# migration 0030's `messages_deny_sequence_number_rewrite` trigger makes overwriting an
# already-assigned ordinal impossible at the database level — but "modifies existing data" is
# still the truthful answer, and this field is reported to the founder, so it is True.
#
# task_execution: `modifies_existing_data`/`writes_new_records` describe MainAI's OWN
# database state (mainai_tasks/mainai_task_events/mainai_checkpoints — new rows, no existing
# document/claim/memory row is ever touched). Whether a given task_execution job also touches
# the EXTERNAL GitHub repo is a separate, per-task question governed by the approval gate
# (app/mainai_execution/approval.py) and MainAITask.approval_required — never merge/deploy/
# production capable at all (see KNOWN_TASK_TYPES in app/mainai_execution/planner.py: no such
# task_type exists for the planner to even propose).
_CAPABILITY_WRITE_PROFILE: dict[str, dict] = {
    "corpus_review": {"modifies_existing_data": False, "writes_new_records": True, "sandbox_only": False, "production_prohibited": False},
    "message_sequence_backfill": {
        "modifies_existing_data": True,
        "writes_new_records": False,
        "sandbox_only": False,
        "production_prohibited": False,
    },
    "structured_export_import": {
        "modifies_existing_data": False,
        "writes_new_records": True,
        "sandbox_only": False,
        "production_prohibited": False,
    },
    "task_execution": {"modifies_existing_data": False, "writes_new_records": True, "sandbox_only": False, "production_prohibited": False},
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
    write-profile entry is a bug in this module, not a reason to guess a permissive default).

    Founder re-review round (PR #36), fourth pass: `sandbox_only`/`production_prohibited` used
    to be pure metadata on `CapabilityStatus` — reported, never enforced. `corpus_review` itself
    sets both `False` so this had no live effect yet, but nothing would have stopped a FUTURE
    capability that set either flag `True` from still running in production, since no code
    path ever read them. Now checked here, in the one function both `require_capability()`
    (job creation, and — as of this same pass — `app/worker.py`'s execution-time re-check) call,
    so job creation and worker execution can never apply a different policy from each other."""
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

    from app.config import get_settings  # local import: this module stays DB/provider-free at import time
    from app.providers.registry import resolve_active

    # `capability not in the dict` (fail closed, stays unconfigured) is deliberately
    # distinguished from `mapped to None` (reviewed as needing no provider at all, therefore
    # always configured) — see _CAPABILITY_PROVIDER_ROLE's own comment. A missing entry must
    # never be silently read as "no provider needed".
    configured = False
    if capability in _CAPABILITY_PROVIDER_ROLE:
        role = _CAPABILITY_PROVIDER_ROLE[capability]
        if role is None:
            configured = True
        else:
            try:
                provider, _model = resolve_active(db, role=role)
                configured = provider.is_configured()
            except Exception:  # noqa: BLE001 - resolve_active/is_configured failing means "not configured", never a 500 here
                configured = False

    profile = _CAPABILITY_WRITE_PROFILE.get(capability, {})
    sandbox_only = bool(profile.get("sandbox_only"))
    production_prohibited = bool(profile.get("production_prohibited"))
    in_production = get_settings().environment == "production"

    unavailable_reason: str | None = None
    if not configured:
        unavailable_reason = "not_configured"
    elif in_production and production_prohibited:
        unavailable_reason = "production_prohibited"
    elif in_production and sandbox_only:
        unavailable_reason = "sandbox_only"

    currently_available = implemented and configured and unavailable_reason is None
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
        unavailable_reason=unavailable_reason,
    )


def require_capability(db, capability: str) -> None:
    """Fail closed: raises CapabilityUnavailableError unless `capability` is implemented,
    currently configured to run, AND not blocked by its own sandbox_only/production_prohibited
    policy in the current environment. This is the ONE central policy function — called both by
    `app/jobs/service.py`'s create_job (job creation) AND, as of the founder
    re-review round's fourth pass, `app/worker.py`'s process_claimed_mainai_job (job execution,
    re-checked immediately before dispatch) — so a capability that becomes unconfigured or
    newly production-prohibited between a job's creation and its actual execution is caught at
    BOTH points the same way, not just at creation time. `db` is required — every call site
    already has a session in scope; never guess a permissive default with no db access."""
    status = get_capability_status(db, capability)
    if not status.currently_available:
        raise CapabilityUnavailableError(capability, reason=status.unavailable_reason or "not_implemented")
