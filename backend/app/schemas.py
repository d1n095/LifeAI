from decimal import Decimal
import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.email_utils import EMAIL_PATTERN, normalize_email


class LoginIn(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, v: str) -> str:
        if not EMAIL_PATTERN.match(v.strip()):
            raise ValueError("Ogiltig e-postadress.")
        return normalize_email(v)

    @field_validator("password")
    @classmethod
    def valid_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Lösenordet måste vara minst 8 tecken.")
        return v


class RegisterIn(BaseModel):
    email: str
    password: str
    # Honeypot: a hidden form field real users never see or fill (frontend keeps it visually
    # and semantically hidden from assistive tech). Any value here marks the submission as
    # automated — see app/routers/auth.py. Actual password strength is validated explicitly
    # in the endpoint (app/password_policy.py), not here, since the Swedish error message
    # needs to reach the client as a normal 400 response, not a generic Pydantic 422.
    website: str = ""

    @field_validator("email")
    @classmethod
    def valid_email(cls, v: str) -> str:
        if not EMAIL_PATTERN.match(v.strip()):
            raise ValueError("Ogiltig e-postadress.")
        return normalize_email(v)


class EmailIn(BaseModel):
    """Shared by /forgot-password and /resend-verification — both take just an email and
    both must respond identically regardless of whether the address is registered."""

    email: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, v: str) -> str:
        if not EMAIL_PATTERN.match(v.strip()):
            raise ValueError("Ogiltig e-postadress.")
        return normalize_email(v)


class VerifyEmailIn(BaseModel):
    token: str


class ResetPasswordIn(BaseModel):
    token: str
    new_password: str


class DeleteAccountIn(BaseModel):
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    role: str
    email_verified: bool


class SessionOut(UserOut):
    """Returned only by login/refresh/me — the one time the CSRF value is transmitted at all.
    The frontend holds it in memory (never localStorage) and echoes it back as
    X-CSRF-Token on mutating requests. See docs/AUTH_THREAT_MODEL.md."""

    csrf_token: str


class ChatMessageIn(BaseModel):
    conversation_id: uuid.UUID | None = None
    # Optional workspace association. When present, the conversation is bound to that
    # owner-scoped project and retrieval is narrowed to its documents.
    project_id: uuid.UUID | None = None
    message: str
    # Optional caller-supplied idempotency key. A retry of the same logical send must reuse
    # this exact value; a new user intent gets a new value. Bounded/opaque so it can be used
    # as a durable dedupe key without becoming a second message-content channel.
    client_message_id: str | None = None

    @field_validator("client_message_id")
    @classmethod
    def valid_client_message_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not re.fullmatch(r"[A-Za-z0-9._~:-]{1,128}", v):
            raise ValueError(
                "client_message_id måste vara 1–128 tecken och endast A-Z, a-z, 0-9, . _ ~ : -."
            )
        return v

    @field_validator("message")
    @classmethod
    def non_empty_and_bounded(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Meddelandet får inte vara tomt.")
        if len(v) > 8000:
            raise ValueError("Meddelandet är för långt (max 8000 tecken).")
        return v


class SourceRef(BaseModel):
    document_id: uuid.UUID
    title: str
    snippet: str
    score: float
    active_truth_status: str | None = None
    # STEG 12: set only when this citation comes from a timed transcript chunk
    # (app/rag/media_import.py) — lets the frontend player open the source at the exact
    # moment instead of just the source itself.
    start_seconds: float | None = None
    end_seconds: float | None = None


class ContextStatusOut(BaseModel):
    """Structured, frontend-renderable reason for why chat retrieval found nothing — see
    app/rag/context_status.py. Deliberately NOT relying on the assistant's own reply wording:
    a founder (or the UI) must be able to see the verified reason even if the model paraphrases
    or ignores it. `message` is always a safe, pre-built sentence — never a raw exception
    string, never a URL/key/header (see classify_provider_exception's own guarantee for the
    `search_provider_unavailable` case)."""

    reason: str  # one of app/rag/context_status.py's ContextStatusReason values
    message: str
    pending_count: int = 0
    awaiting_provider_count: int = 0
    failed_count: int = 0
    indexed_count: int = 0
    total_document_count: int = 0
    worker_reachable: bool | None = None
    # Chunks that exist but are not in the active embedding space (another model, or a
    # pre-migration NULL identity) — see app/rag/context_status.py's reindex_required.
    out_of_space_chunk_count: int = 0


class ChatMessageOut(BaseModel):
    """The explicit user-message-vs-assistant-response contract (see app/routers/chat.py's
    module docstring): `user_message_saved` is always true if this response exists at all —
    the user's message was committed to the database before the provider was ever called, so
    a client must never infer "my message is gone" from a non-200 or from `assistant_status`
    being "failed". `reply`/`provider`/`model`/`sources`/`confidence*` are only meaningful
    when `assistant_status == "succeeded"` — they are None on failure, never a fake/empty
    placeholder pretending a reply exists."""

    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    user_message_saved: bool = True
    assistant_status: str  # "succeeded" | "failed" — see MessageStatus
    assistant_message_id: uuid.UUID | None = None
    reply: str | None = None
    provider: str | None = None
    model: str | None = None
    sources: list[SourceRef] = []
    confidence: str | None = None  # "high" | "medium" | "low" | "none" — set only on success
    confidence_score: float | None = None
    providers_attempted: list[str] = []  # >1 entry means fallback engaged
    conflicts_detected: bool = False
    context_intent: str | None = None  # see app/context/resolver.py — observational only tonight
    context_confidence: str | None = None
    # Set whenever retrieval found zero hits (success or failure path) — see
    # app/rag/context_status.py. None when hits were actually found; there is nothing to
    # explain in that case.
    context_status: ContextStatusOut | None = None
    # Only set when assistant_status == "failed". error_category is one of
    # VerificationResult's values (app/models/provider_verification.py) — never a raw
    # exception string, so it's always safe to show a founder without leaking a key/URL.
    error_category: str | None = None
    error_message: str | None = None
    retryable: bool = False
    # R15-03: grounding is never silently absent. "grounded" means real retrieval hits were
    # used; "degraded" means retrieval could not run (the reason is safe and explicit);
    # "empty" means retrieval ran but matched nothing; "unknown" is used on replay paths
    # where the original retrieval outcome was not persisted. See app/routers/chat.py.
    grounding_status: str = "unknown"
    grounding_reason: str | None = None


class ChatUnresolvedAttemptOut(BaseModel):
    """One billable chat attempt whose provider outcome is not yet reconciled.

    Safe metadata only: no prompt/reply text, no reservation ids, no provider credentials.
    `release_allowed` is true only for a stale attempt that is provably pre-egress; every
    ambiguous attempt requires a conservative settlement or recorded usage.
    """

    id: uuid.UUID
    assistant_message_id: uuid.UUID
    user_message_id: uuid.UUID
    state: str
    effect_boundary: str | None = None
    provider: str
    model: str
    created_at: datetime
    updated_at: datetime
    age_seconds: int
    release_allowed: bool
    review_note: str


class ChatAttemptReconcileIn(BaseModel):
    resolution: Literal["release_pre_egress", "settle_conservative", "settle_from_usage"]
    evidence_ref: str = Field(min_length=8, max_length=256)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    role: str
    content: str
    provider: str | None
    model: str | None
    created_at: datetime
    # A reloaded conversation must preserve the same failure state the live response showed:
    # a failed assistant row has empty content, so without these fields the UI silently drops
    # it and the founder cannot see that generation failed or retry it.
    status: str = "succeeded"
    error_category: str | None = None
    in_reply_to_id: uuid.UUID | None = None

    @field_validator("status", mode="before")
    @classmethod
    def status_value(cls, value):
        return getattr(value, "value", value)


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID | None = None
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationDetailOut(ConversationOut):
    messages: list[MessageOut] = []


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    source: str
    category: str | None
    status: str
    chunk_count: int
    created_at: datetime


class ProjectIn(BaseModel):
    name: str
    description: str | None = None
    status: str = "idea"


class ProjectOut(ProjectIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    created_at: datetime


_TASK_STATUSES = {"todo", "in_progress", "blocked", "done", "cancelled"}
_TASK_PRIORITIES = {"low", "medium", "high", "critical"}


def _validate_task_status(value: str) -> str:
    if value not in _TASK_STATUSES:
        raise ValueError(f"status måste vara en av: {', '.join(sorted(_TASK_STATUSES))}.")
    return value


def _validate_task_priority(value: str) -> str:
    if value not in _TASK_PRIORITIES:
        raise ValueError(f"priority måste vara en av: {', '.join(sorted(_TASK_PRIORITIES))}.")
    return value


class TaskIn(BaseModel):
    project_id: uuid.UUID | None = None
    title: str = Field(min_length=1, max_length=512)
    description: str | None = Field(default=None, max_length=20_000)
    status: str = "todo"
    priority: str = "medium"
    due_at: datetime | None = None
    milestone_id: uuid.UUID | None = None
    depends_on_task_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def clean_task_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Uppgiftens titel får inte vara tom.")
        return value

    @field_validator("status")
    @classmethod
    def valid_task_status(cls, value: str) -> str:
        return _validate_task_status(value)

    @field_validator("priority")
    @classmethod
    def valid_task_priority(cls, value: str) -> str:
        return _validate_task_priority(value)


class TaskUpdate(BaseModel):
    expected_version: int | None = Field(default=None, ge=1)
    project_id: uuid.UUID | None = None
    title: str | None = Field(default=None, min_length=1, max_length=512)
    description: str | None = Field(default=None, max_length=20_000)
    status: str | None = None
    priority: str | None = None
    due_at: datetime | None = None
    milestone_id: uuid.UUID | None = None
    blocked_reason: str | None = Field(default=None, max_length=512)
    depends_on_task_ids: list[uuid.UUID] | None = None

    @field_validator("title")
    @classmethod
    def clean_optional_task_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Uppgiftens titel får inte vara tom.")
        return value

    @field_validator("status")
    @classmethod
    def valid_optional_task_status(cls, value: str | None) -> str | None:
        return None if value is None else _validate_task_status(value)

    @field_validator("priority")
    @classmethod
    def valid_optional_task_priority(cls, value: str | None) -> str | None:
        return None if value is None else _validate_task_priority(value)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID | None = None
    title: str
    description: str | None = None
    status: str
    priority: str
    due_at: datetime | None = None
    completed_at: datetime | None = None
    blocked_reason: str | None = None
    milestone_id: uuid.UUID | None = None
    version: int = 1
    suggested_by_ai: bool = False
    created_at: datetime
    updated_at: datetime | None = None
    is_blocked: bool = False
    dependency_count: int = 0


class MilestoneIn(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=20_000)
    due_at: datetime | None = None
    status: str = "open"

    @field_validator("title")
    @classmethod
    def clean_milestone_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Milstolpens titel får inte vara tom.")
        return value

    @field_validator("status")
    @classmethod
    def valid_milestone_status(cls, value: str) -> str:
        if value not in {"open", "in_progress", "completed", "cancelled"}:
            raise ValueError("status måste vara en av: open, in_progress, completed, cancelled.")
        return value


class MilestoneUpdate(BaseModel):
    expected_version: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=20_000)
    due_at: datetime | None = None
    status: str | None = None

    @field_validator("title")
    @classmethod
    def clean_optional_milestone_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Milstolpens titel får inte vara tom.")
        return value

    @field_validator("status")
    @classmethod
    def valid_optional_milestone_status(cls, value: str | None) -> str | None:
        if value is not None and value not in {"open", "in_progress", "completed", "cancelled"}:
            raise ValueError("status måste vara en av: open, in_progress, completed, cancelled.")
        return value


class MilestoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    description: str | None = None
    due_at: datetime | None = None
    status: str
    completed_at: datetime | None = None
    version: int = 1
    created_at: datetime
    updated_at: datetime
    task_count: int = 0
    completed_task_count: int = 0


class TaskDependencyOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    depends_on_task_id: uuid.UUID
    depends_on_title: str
    depends_on_status: str


class TaskEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    event_type: str
    from_status: str | None = None
    to_status: str | None = None
    detail: dict = Field(default_factory=dict)
    created_at: datetime


class TaskDetailOut(TaskOut):
    dependencies: list[TaskDependencyOut] = Field(default_factory=list)
    dependents: list[TaskDependencyOut] = Field(default_factory=list)
    events: list[TaskEventOut] = Field(default_factory=list)
    milestone: MilestoneOut | None = None
    project_name: str | None = None
    kind: Literal["user_task"] = "user_task"
    execution_authority: Literal["none"] = "none"


class CompanyInfoIn(BaseModel):
    key: str
    label: str
    content: str


class CompanyInfoOut(CompanyInfoIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    updated_at: datetime


class ProviderConfigIn(BaseModel):
    role: str  # "chat" | "embedding"
    provider: str
    model: str

    @field_validator("role")
    @classmethod
    def valid_role(cls, value: str) -> str:
        if value not in ("chat", "embedding"):
            raise ValueError("role måste vara 'chat' eller 'embedding'.")
        return value

    @field_validator("provider", "model")
    @classmethod
    def non_empty_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("provider och model måste vara icke-tomma.")
        return normalized


class ProviderConfigOut(ProviderConfigIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    is_active: bool


class ModelCatalogOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    supported_capabilities: list[str]
    metadata_note: str = "declared metadata; not runtime, price, quota or version guarantees"
    provider: str
    model_id: str
    version: str
    capabilities: list[str]
    context_window_tokens: int | None
    locality: str
    license_name: str
    license_url: str | None
    input_price_per_million_usd: float | None
    output_price_per_million_usd: float | None
    pricing_source: str
    pricing_as_of: str | None
    requests_per_minute: int | None
    tokens_per_minute: int | None
    quota_note: str
    availability: str
    configured: bool
    verification_result: str | None = None
    verification_checked_at: datetime | None = None


class ModelSelectionPreviewIn(BaseModel):
    role: str = "chat"
    required_capabilities: list[str] = []
    minimum_context_tokens: int = 0
    estimated_input_tokens: int = 0
    allow_external: bool = False
    prefer_local: bool = True
    allowed_providers: list[str] = []
    allowed_models: list[str] = []
    max_input_price_per_million_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_output_price_per_million_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    estimated_output_tokens: int = Field(default=0, ge=0)
    max_pricing_age_days: int = Field(default=30, ge=0, le=365)
    require_verified: bool = True
    requested_provider: str | None = None
    requested_model: str | None = None

    @field_validator("role")
    @classmethod
    def valid_selection_role(cls, value: str) -> str:
        if value not in ("chat", "embedding"):
            raise ValueError("role måste vara 'chat' eller 'embedding'.")
        return value

    @field_validator("minimum_context_tokens", "estimated_input_tokens")
    @classmethod
    def non_negative_tokens(cls, value: int) -> int:
        if value < 0:
            raise ValueError("tokenvärden måste vara icke-negativa.")
        return value


class ModelSelectionPreviewOut(BaseModel):
    provider: str
    model: str
    version: str
    capabilities: list[str]
    locality: str
    verified_required: bool
    authority_note: str = (
        "selection is not spend, egress, execution, merge, deploy, or activation authority"
    )


class ProviderVerificationOut(BaseModel):
    """P1: the latest real verification result for one (provider, role) pair — never just
    whether a key string is present. `None` (the field is omitted on the Python side, not
    this model) means "never verified yet", surfaced by the frontend as a neutral, not red,
    state."""

    result: str  # ok | invalid_key | unreachable | rate_limited | unsupported | not_configured
    message: str
    checked_by: str  # "system" | "founder"
    checked_at: datetime
    model: str | None = None


class ProviderStatus(BaseModel):
    name: str
    configured: bool
    active_chat: bool
    active_embedding: bool
    chat_verification: ProviderVerificationOut | None = None
    embedding_verification: ProviderVerificationOut | None = None


class ProviderVerifyIn(BaseModel):
    provider: str
    role: str  # "chat" | "embedding"
    model: str | None = None

    @field_validator("provider", "model")
    @classmethod
    def valid_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value or len(value) > 128 or any(ord(c) < 32 for c in value):
            raise ValueError("ogiltigt provider-/modellnamn")
        return value

    @field_validator("role")
    @classmethod
    def valid_role(cls, v: str) -> str:
        if v not in ("chat", "embedding"):
            raise ValueError("role måste vara 'chat' eller 'embedding'.")
        return v


class UsageSummaryRow(BaseModel):
    provider: str
    model: str
    role: str
    request_count: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None  # None means at least one row in this group has unknown pricing


# --- Founder Knowledge Studio v1 (see docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md) ---


class KnowledgeVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    version_number: int
    checksum: str
    extraction_version: str
    raw_metadata: dict | None
    created_at: datetime


class SourceRelationshipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    from_source_id: uuid.UUID
    to_source_id: uuid.UUID
    relationship_type: str
    note: str | None
    created_at: datetime


class SourceRelationshipIn(BaseModel):
    to_source_id: uuid.UUID
    relationship_type: str
    note: str | None = None

    @field_validator("relationship_type")
    @classmethod
    def valid_relationship_type(cls, v: str) -> str:
        allowed = {"derived_from", "supersedes", "contradicts", "supports", "duplicates", "belongs_to"}
        if v not in allowed:
            raise ValueError(f"Ogiltig relationstyp. Måste vara en av: {', '.join(sorted(allowed))}.")
        return v


class KnowledgeSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    source: str
    media_type: str | None
    original_filename: str | None
    category: str | None
    classification: str
    active_truth_status: str
    status: str
    chunk_count: int
    checksum: str | None
    project_id: uuid.UUID | None
    version_number: int
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    imported_at: datetime | None
    media_duration_seconds: float | None = None
    transcript_provider: str | None = None


class ProjectDetailOut(ProjectOut):
    """A project and the scoped collections the founder actually needs to inspect it:
    its own tasks, milestones and non-deleted knowledge sources. Every list is filtered by
    the project id server-side; the frontend never derives project membership from a global
    list."""

    tasks: list[TaskOut] = []
    milestones: list[MilestoneOut] = []
    sources: list[KnowledgeSourceOut] = []


# --- Life Workspaces (persistent project-centered founder experience) ----------------------

_WORKSPACE_STATUSES = {"idea", "active", "paused", "done"}
_WORKSPACE_DECISION_STATUSES = {"proposed", "accepted", "superseded"}


def _bounded_goals(value: list[str]) -> list[str]:
    cleaned = [item.strip() for item in value if item and item.strip()]
    if len(cleaned) > 50:
        raise ValueError("Ett workspace får ha högst 50 mål.")
    if any(len(item) > 500 for item in cleaned):
        raise ValueError("Ett mål får vara högst 500 tecken.")
    return cleaned


class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=20_000)
    purpose: str | None = Field(default=None, max_length=20_000)
    goals: list[str] = Field(default_factory=list)
    status: str = "idea"
    settings: dict = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Workspace-namnet får inte vara tomt.")
        return value

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        if value not in _WORKSPACE_STATUSES:
            raise ValueError(f"status måste vara en av: {', '.join(sorted(_WORKSPACE_STATUSES))}.")
        return value

    @field_validator("goals")
    @classmethod
    def valid_goals(cls, value: list[str]) -> list[str]:
        return _bounded_goals(value)


class WorkspaceUpdate(BaseModel):
    # Optional compare-and-set guard. A stale value returns 409 instead of overwriting a
    # newer workspace edit; omitted keeps the existing last-write-wins compatibility path.
    expected_version: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=20_000)
    purpose: str | None = Field(default=None, max_length=20_000)
    goals: list[str] | None = None
    status: str | None = None
    settings: dict | None = None

    @field_validator("name")
    @classmethod
    def clean_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Workspace-namnet får inte vara tomt.")
        return value

    @field_validator("status")
    @classmethod
    def valid_optional_status(cls, value: str | None) -> str | None:
        if value is not None and value not in _WORKSPACE_STATUSES:
            raise ValueError(f"status måste vara en av: {', '.join(sorted(_WORKSPACE_STATUSES))}.")
        return value

    @field_validator("goals")
    @classmethod
    def valid_optional_goals(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _bounded_goals(value)


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    version: int = 1
    name: str
    description: str | None = None
    purpose: str | None = None
    goals: list[str] = Field(default_factory=list)
    status: str
    settings: dict = Field(default_factory=dict)
    archived_at: datetime | None = None
    last_activity_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class WorkspaceActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: str
    summary: str
    detail: dict = Field(default_factory=dict)
    created_at: datetime


class WorkspaceSyncIn(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def valid_idempotency_key(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9._~:-]{1,128}", value):
            raise ValueError(
                "idempotency_key måste vara 1–128 tecken och endast A-Z, a-z, 0-9, . _ ~ : -."
            )
        return value


class WorkspaceSyncItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    entity_version: int
    content_hash: str
    status: str
    error: str | None = None
    processed_at: datetime | None = None
    created_at: datetime


class WorkspaceSyncRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    status: str
    snapshot_hash: str
    snapshot_version: int
    progress_current: int
    progress_total: int
    attempt: int
    error_category: str | None = None
    result: dict = Field(default_factory=dict)
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    heartbeat_at: datetime | None = None
    items: list[WorkspaceSyncItemOut] = Field(default_factory=list)


class WorkspaceDecisionIn(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    decision: str = Field(min_length=1, max_length=20_000)
    rationale: str | None = Field(default=None, max_length=20_000)
    status: str = "accepted"
    epistemic_status: str = "confirmed"
    origin: str = "founder"

    @field_validator("status")
    @classmethod
    def valid_decision_status(cls, value: str) -> str:
        if value not in _WORKSPACE_DECISION_STATUSES:
            raise ValueError(f"status måste vara en av: {', '.join(sorted(_WORKSPACE_DECISION_STATUSES))}.")
        return value

    @field_validator("epistemic_status")
    @classmethod
    def valid_epistemic_status(cls, value: str) -> str:
        if value not in {"confirmed", "uncertain", "proposed"}:
            raise ValueError("epistemic_status måste vara confirmed, uncertain eller proposed.")
        return value

    @field_validator("origin")
    @classmethod
    def valid_origin(cls, value: str) -> str:
        if value not in {"founder", "ai"}:
            raise ValueError("origin måste vara founder eller ai.")
        return value


class WorkspaceDecisionSupersedeIn(WorkspaceDecisionIn):
    pass


class WorkspaceDecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    decision: str
    rationale: str | None = None
    status: str
    version: int = 1
    supersedes_id: uuid.UUID | None = None
    epistemic_status: str = "confirmed"
    origin: str = "founder"
    superseded_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class WorkspaceContextSourceOut(BaseModel):
    document_id: uuid.UUID
    title: str
    snippet: str
    score: float
    active_truth_status: str | None = None


class WorkspaceContextOut(BaseModel):
    workspace_id: uuid.UUID
    context_version: int = 1
    objectives: list[str]
    milestones: list[MilestoneOut] = Field(default_factory=list)
    source_documents: list[KnowledgeSourceOut]
    decisions: list[WorkspaceDecisionOut]
    outstanding_tasks: list[TaskOut]
    past_conversations: list[ConversationOut]
    recent_changes: list[WorkspaceActivityOut]
    uncertainties: list[dict]
    retrieval_sources: list[WorkspaceContextSourceOut] = Field(default_factory=list)
    retrieval_degraded_reason: str | None = None
    bounded: bool = True


class WorkspaceDetailOut(WorkspaceOut):
    tasks: list[TaskOut] = Field(default_factory=list)
    milestones: list[MilestoneOut] = Field(default_factory=list)
    sources: list[KnowledgeSourceOut] = Field(default_factory=list)
    conversations: list[ConversationOut] = Field(default_factory=list)
    activities: list[WorkspaceActivityOut] = Field(default_factory=list)
    decisions: list[WorkspaceDecisionOut] = Field(default_factory=list)
    context: WorkspaceContextOut


class KnowledgeClaimOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    claim_text: str
    claim_type: str  # P3: idea | decision | task_reference | vision | technical | historical | uncategorized
    status: str
    confidence: str  # computed via app/rag/trust.py's assess_claim_confidence(), not the stored extraction-time value
    grounding_score: float
    chunk_id: uuid.UUID | None
    created_at: datetime
    # Evidence-aware epistemics (app/rag/epistemics.py): verified_fact |
    # conflicting_evidence | hypothesis | assumption | unknown — additive; None only for
    # constructors that predate the ladder (every live read path sets it).
    epistemic_status: str | None = None
    # Evidence-quality metadata (app/rag/evidence_metadata.py): the founder-declared study
    # facts and the MATERIALIZED verdict the pure grader computed from them. None/[] when
    # nothing has been stamped for the claim — an honest absence, never a guess.
    quality_grade: str | None = None
    quality_cap: str | None = None
    quality_caveats: list[str] = []


class ClaimEvidenceMetadataIn(BaseModel):
    """A founder-declared structured study-metadata block for one claim (the producer
    input to app/rag/evidence_metadata.record_claim_evidence_metadata). Keys outside the
    declared vocabulary come back in `rejected_keys` — fail-closed, never stored."""

    metadata: dict = {}
    declared_via: str = "founder_api"


class ClaimEvidenceMetadataOut(BaseModel):
    claim_id: uuid.UUID
    declared: dict
    rejected: dict[str, str]
    quality_grade: str
    quality_cap: str
    quality_caveats: list[str]
    axis_findings: list[dict]
    declared_via: str
    recorded_at: datetime


class ClaimGraphEdgeIn(BaseModel):
    """A founder-declared claim-graph edge (Round 8 M3): the production input path for
    connecting claims across domains. The type vocabulary is the table's own closed enum;
    an unknown type is refused, never guessed."""

    to_claim_id: uuid.UUID
    relationship_type: str  # supports | contradicts | supersedes | duplicates
    note: str | None = None


class ClaimGraphEdgeOut(BaseModel):
    id: uuid.UUID
    from_claim_id: uuid.UUID
    to_claim_id: uuid.UUID
    relationship_type: str
    note: str | None = None
    # Epistemic-shift records (Round 8 M3): prior vs current ladder verdict for each
    # endpoint, computed when a contradicts edge lands — "new information changed an
    # earlier conclusion", with the reasons on both sides. Empty for non-contradicts edges.
    epistemic_shifts: list[dict] = []


class ClaimEvidenceChainOut(BaseModel):
    """The full traceability record for one claim (Round 8 M3): verdict + reasons + the
    source/chunk grounding + BOTH sides of every contradicting edge (a conflict is
    presented, never adjudicated here) + the stamped study-metadata verdict + the
    bounded second-order consequences of the claim's source. Round 10 M4 adds the
    bounded INTELLECTUAL ADVERSARY review: evidence-backed objections when justified,
    each with its epistemic category and provenance pointers; reviewed=False means no
    justified objection — never silent approval."""

    claim_id: uuid.UUID
    claim_text: str
    claim_type: str
    claim_status: str
    confidence: str
    epistemic_status: str
    verdict_reasons: list[str]
    source: dict
    chunk_grounded: bool
    adversarial_review: dict
    quality_metadata: dict | None = None
    supporting_edges: list[dict] = []
    contradicting_edges: list[dict] = []
    other_edges: list[dict] = []
    second_order_consequences: list[dict] = []


class MediaSegmentOut(BaseModel):
    """STEG 13: the FULL timestamped chunk list for a media source — deliberately separate
    from chunk_preview (which stays truncated to CHUNK_PREVIEW_COUNT/CHUNK_PREVIEW_LENGTH
    for text sources) since the Library UI's transcript view/search needs every chunk, not a
    preview, to be useful."""

    chunk_index: int
    text: str
    start_seconds: float | None
    end_seconds: float | None


class KnowledgeSourceDetailOut(KnowledgeSourceOut):
    versions: list[KnowledgeVersionOut] = []
    relationships: list[SourceRelationshipOut] = []
    chunk_preview: list[str] = []
    claims: list[KnowledgeClaimOut] = []
    segments: list[MediaSegmentOut] = []


class ImportJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    status: str
    source_filename: str | None
    source_checksum: str | None
    progress_current: int
    progress_total: int
    succeeded_count: int
    failed_count: int
    skipped_count: int
    # 2026-07-28: was already tracked on the model (app/models/import_job.py) but never
    # exposed here — the Library UI had no way to tell "13 files failed, 191 are still
    # waiting on the provider" from a `partial` job without it, so a partial success looked
    # identical to a plain "some failed" job.
    blocked_count: int
    failure_reason: str | None
    manifest: dict | None
    file_results: list | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    attempt_count: int
    max_attempts: int
    last_failure_transient: bool | None


class SourceReplacementOut(BaseModel):
    """Result of replacing a Library source with new bytes.

    The old source is kept for provenance (its versions, claims and original blob), but its
    `active_truth_status` becomes `superseded` and a `supersedes` edge from the new source
    makes it unreachable through both retrieval channels. The new source starts a fresh
    import job and continues the source's version numbering.
    """

    source: KnowledgeSourceOut
    job: ImportJobOut
    superseded_source_id: uuid.UUID
    version_number: int


class OpsStatusOut(BaseModel):
    """Life Library durable-worker package (DEL 6): founder-only operational status —
    aggregated counts/booleans/timestamps only, deliberately never a private filesystem path
    or any secret (storage_root itself, connection strings, etc. never appear here — see
    app/routers/library.py's ops_status())."""

    worker_reachable: bool
    queue_length: int
    running_jobs: int
    oldest_pending_age_seconds: float | None
    failed_last_24h: int
    storage_writable: bool
    free_disk_bytes: int | None
    last_heartbeat_at: datetime | None
    # Pass 32 (an eighth founder review round): a durable AuditLog(action='storage_orphan_
    # risk') row on its own is not "founder ops-status can see this" -- these fields are what
    # actually surfaces it here. See app/storage/references.py's
    # get_storage_cleanup_ops_status() for the exact degradation policy (in particular: why
    # storage_orphan_risk_count never self-clears yet, while failed_storage_cleanup_tasks
    # does). Deliberately never a raw storage_key -- counts and timestamps only, same
    # "aggregated only" contract as every other field on this model.
    storage_cleanup_degraded: bool
    storage_orphan_risk_count: int
    latest_storage_orphan_risk_at: datetime | None
    pending_storage_cleanup_tasks: int
    failed_storage_cleanup_tasks: int
    oldest_failed_storage_cleanup_age_seconds: float | None


class LibrarySearchHit(BaseModel):
    document_id: uuid.UUID
    title: str
    text: str
    score: float
    classification: str
    active_truth_status: str
    media_type: str | None
    text_match: bool = False
    start_seconds: float | None = None
    end_seconds: float | None = None


class LibrarySearchResponseOut(BaseModel):
    """Wraps LibrarySearchHit results with explicit degradation metadata — the search
    failure boundary. semantic_search_available=False means the embedding provider was
    unreachable and `results` came from the text-match (ILIKE) channel alone; the client
    must never treat that silently as "semantic search ran and found nothing", since those
    are different situations (see app/routers/library.py's search_library)."""

    results: list[LibrarySearchHit]
    semantic_search_available: bool = True
    degraded_reason: str | None = None


# --- STEG 12: secure URL-import model (intent only, never fetched — see
# app/models/media_url_import.py) ---

MEDIA_URL_PLATFORMS = {"youtube", "vimeo", "generic"}


class MediaUrlImportIn(BaseModel):
    url: str
    platform: str
    consent_confirmed: bool = False
    rights_note: str | None = None
    project_id: uuid.UUID | None = None

    @field_validator("url")
    @classmethod
    def http_url_only(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("URL får inte vara tom.")
        if len(v) > 2000:
            raise ValueError("URL:en är för lång (max 2000 tecken).")
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("Endast http(s)-URL:er stöds.")
        return v

    @field_validator("platform")
    @classmethod
    def known_platform(cls, v: str) -> str:
        if v not in MEDIA_URL_PLATFORMS:
            raise ValueError(f"Okänd plattform. Tillåtna: {', '.join(sorted(MEDIA_URL_PLATFORMS))}.")
        return v

    @field_validator("rights_note")
    @classmethod
    def bounded_rights_note(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 4000:
            raise ValueError("Rättighetsnoteringen är för lång (max 4000 tecken).")
        return v


class MediaUrlImportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    url: str
    platform: str
    consent_confirmed: bool
    rights_note: str | None
    status: str
    project_id: uuid.UUID | None
    created_at: datetime


class DeleteConfirmIn(BaseModel):
    confirm: bool = False


# --- Founder Workbench (DEL 9) ---


class WorkbenchAnalyzeIn(BaseModel):
    question: str
    project_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None


class WorkbenchAnalyzeOut(BaseModel):
    question: str
    conclusion: str
    critique: str | None
    sources: list[SourceRef]
    confidence: str
    confidence_score: float
    conflicts_detected: bool
    provider: str
    model: str


class WorkbenchSaveIn(BaseModel):
    question: str
    conclusion: str
    critique: str | None = None
    label: str
    project_id: uuid.UUID | None = None
    source_document_ids: list[uuid.UUID] = []

    @field_validator("label")
    @classmethod
    def valid_label(cls, v: str) -> str:
        allowed = {"idea", "proposal", "decision", "history"}
        if v not in allowed:
            raise ValueError(f"Ogiltig etikett. Måste vara en av: {', '.join(sorted(allowed))}.")
        return v


_NOTE_KINDS = ("fact", "decision", "blocker", "next_step", "uncertainty", "idea")
_SIDE_ISSUE_CLASSIFICATIONS = ("blocking", "directly_resolvable", "registered_for_later", "needs_founder_decision")


class ProjectNoteIn(BaseModel):
    kind: str  # "fact" | "decision" | "blocker" | "next_step" | "uncertainty"
    content: str
    source_type: str
    source_ref: str
    source_id: uuid.UUID | None = None
    classification: str | None = None  # see _SIDE_ISSUE_CLASSIFICATIONS

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, v: str) -> str:
        if v not in _NOTE_KINDS:
            raise ValueError(f"kind måste vara en av: {', '.join(_NOTE_KINDS)}.")
        return v

    @field_validator("classification")
    @classmethod
    def valid_classification(cls, v: str | None) -> str | None:
        if v is not None and v not in _SIDE_ISSUE_CLASSIFICATIONS:
            raise ValueError(f"classification måste vara en av: {', '.join(_SIDE_ISSUE_CLASSIFICATIONS)}.")
        return v


class ProjectNoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: str
    status: str
    content: str
    source_type: str
    source_ref: str
    source_id: uuid.UUID | None = None
    classification: str | None = None
    created_by: str
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution_note: str | None = None


class ProjectNoteResolveIn(BaseModel):
    resolution_note: str
    superseded: bool = False


class ProjectNoteClassifyIn(BaseModel):
    classification: str

    @field_validator("classification")
    @classmethod
    def valid_classification(cls, v: str) -> str:
        if v not in _SIDE_ISSUE_CLASSIFICATIONS:
            raise ValueError(f"classification måste vara en av: {', '.join(_SIDE_ISSUE_CLASSIFICATIONS)}.")
        return v


class ProjectCheckpointIn(BaseModel):
    summary: str
    branch_name: str
    open_pr_refs: list[str] = []


class ProjectCheckpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    summary: str
    branch_name: str
    open_pr_refs: str
    brief_storage_key: str
    brief_sha256: str
    git_commit_sha: str | None = None
    created_by: str
    created_at: datetime


class ProjectCheckpointDetailOut(ProjectCheckpointOut):
    brief: str


class ProjectCheckpointStaleOut(BaseModel):
    stale: bool
    reasons: list[str]


class ProjectSourceIngestDocIn(BaseModel):
    relative_path: str


class ProjectSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    source_type: str
    source_ref: str
    content_sha256: str | None = None
    storage_key: str | None = None
    commit_sha: str | None = None
    raw_data: dict | None = None
    ingested_by: str
    ingested_at: datetime


class ProjectSourceDetailOut(ProjectSourceOut):
    content: str


class ProjectGithubStatusIn(BaseModel):
    kind: str  # "branch" | "pr"
    ref: str
    status: str
    title: str | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    mergeable: bool | None = None
    ci_status: str | None = None
    summary: str | None = None

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, v: str) -> str:
        if v not in ("branch", "pr"):
            raise ValueError("kind måste vara 'branch' eller 'pr'.")
        return v


class ProjectBranchPRStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: str
    ref: str
    title: str | None = None
    status: str
    base_ref: str | None = None
    head_ref: str | None = None
    mergeable: bool | None = None
    ci_status: str | None = None
    summary: str | None = None
    is_current: bool
    recorded_by: str
    recorded_at: datetime
    superseded_at: datetime | None = None


class ProjectConflictsOut(BaseModel):
    duplicate_work_candidates: list[dict]
    data_integrity_issues: list[dict]


# --- MainAI Core: retrieval + system map + agent orchestration (2026-07-26) ------------------


class RetrieveContextIn(BaseModel):
    query: str = ""
    limit_per_category: int = 5


class AgentTaskCreateIn(BaseModel):
    title: str
    description: str
    acceptance_criteria: str
    target_files: list[str] = []
    constraints: str | None = None
    required_tests: str | None = None
    source_note_id: uuid.UUID | None = None


class AgentTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    description: str
    target_files: list[str]
    constraints: str | None = None
    acceptance_criteria: str
    required_tests: str | None = None
    status: str
    source_note_id: uuid.UUID | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class AgentTaskEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    task_id: uuid.UUID
    event_type: str
    role: str | None = None
    provider: str | None = None
    model: str | None = None
    payload: dict
    created_by: str
    created_at: datetime


class AgentTaskDetailOut(AgentTaskOut):
    events: list[AgentTaskEventOut]


class RecordTestResultsIn(BaseModel):
    passed: bool
    output: str


class PrepareGithubPrIn(BaseModel):
    branch_name: str
    base_branch: str


class BackfillRunCreateIn(BaseModel):
    mode: str  # "dry_run" | "real" — validated against BackfillRunMode by the router


class BackfillRunFailureOut(BaseModel):
    claim_id: uuid.UUID
    reason: str
    attempt_count: int
    created_at: datetime
    updated_at: datetime


class BackfillRunOut(BaseModel):
    id: uuid.UUID
    mode: str
    status: str
    batch_size: int
    total_candidates_snapshot: int
    already_done_snapshot: int
    processed_count: int
    exact_chunk_count: int
    degraded_version_count: int
    missing_document_only_count: int
    skipped_unresolvable_count: int
    failed_count: int
    batches_completed: int
    last_cursor_created_at: datetime | None
    last_cursor_claim_id: uuid.UUID | None
    error_summary: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


# --- MainAI Runtime Truthfulness and Durable Job Foundation (migrations 0026/0027,
# renumbered during integration from the frozen branch's own 0025/0026) ---


class MainAIJobCreateIn(BaseModel):
    job_type: str
    # [{"type": "document", "id": "<uuid>"}, ...] — validated against the caller's own
    # documents by app/jobs/service.py's create_job, never trusted as-is.
    input_refs: list[dict]
    idempotency_key: str | None = None


class MainAIJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    # Only ever populated by the admin endpoint (GET /api/mainai/jobs/admin/all) — the
    # normal, per-owner endpoints don't need it (RLS already scopes every row to exactly one
    # owner, the caller), so it stays None there rather than leaking an id nobody asked for.
    owner_id: uuid.UUID | None = None
    job_type: str
    status: str
    created_at: datetime
    started_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    completed_at: datetime | None = None
    progress_current: int
    progress_total: int | None = None
    current_phase: str | None = None
    public_message: str | None = None
    error_category: str | None = None
    retry_count: int
    max_retries: int
    input_refs: list
    output_refs: list
    provider: str | None = None
    model: str | None = None
    cancel_requested: bool
    cancel_acknowledged: bool
    created_by: str


class MainAIJobEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    job_id: uuid.UUID
    event_type: str
    detail: dict
    created_at: datetime


class MainAIJobDetailOut(MainAIJobOut):
    events: list[MainAIJobEventOut]


class MainAIJobProposalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    job_id: uuid.UUID
    source_document_id: uuid.UUID | None = None
    source_chunk_id: uuid.UUID | None = None
    proposal_type: str
    proposal_text: str
    status: str
    created_at: datetime


# --- MainAI Execution Loop V0.1 (migration 0032, app/mainai_execution/*) -- the minimal
# read/act surface over Goal/Plan/Task the founder's own required list names: create goal,
# read goal, read current plan, list/read tasks, inspect liveness/checkpoint/verification/
# approvals/blockers, read final report, grant/reject approval. See
# app/routers/mainai_execution.py.


class MainAIGoalCreateIn(BaseModel):
    title: str
    original_instruction: str
    risk_level: str = "low"
    approval_policy: str = "standard_repo_work"
    # Optional Life Workspace association; ownership is verified by the router before use.
    project_id: uuid.UUID | None = None


class MainAIGoalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID | None = None
    title: str
    status: str
    risk_level: str
    approval_policy: str
    current_plan_version: int
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    final_outcome: str | None = None


class MainAIPlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    version: int
    status: str
    rationale: str
    created_at: datetime


class MainAITaskEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    event_type: str
    detail: dict
    created_at: datetime


class MainAICheckpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    plan_version: int
    executor_state: dict
    test_status: str | None = None
    ci_status: str | None = None
    blocker: str | None = None
    created_at: datetime


class MainAITaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    goal_id: uuid.UUID
    plan_id: uuid.UUID
    description: str
    task_type: str
    status: str
    priority: int
    risk_level: str
    approval_required: bool
    verification_plan: list
    attempts: int
    max_attempts: int
    blocker_reason: str | None = None
    mainai_job_id: uuid.UUID | None = None
    # V0.3 (migration 0036): when this task becomes eligible for the worker's automatic
    # retry-with-backoff scan -- NULL means no automatic retry is currently scheduled.
    next_retry_at: datetime | None = None
    # Not an ORM column -- computed by app/routers/mainai_execution.py's _task_out() via
    # app/mainai_execution/liveness.py's task_liveness() and set AFTER model_validate(), since
    # the MainAITask ORM object itself has no `liveness` attribute for from_attributes mapping
    # to find. The default here exists only so model_validate(task) doesn't fail on a missing
    # required field before that override happens.
    liveness: str = ""
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class MainAITaskDependencyOut(BaseModel):
    task_id: uuid.UUID
    depends_on_task_id: uuid.UUID


class MainAITaskDetailOut(MainAITaskOut):
    events: list[MainAITaskEventOut]
    checkpoints: list[MainAICheckpointOut]
    depends_on: list[uuid.UUID]
    approval_granted: bool


class MainAIGoalDetailOut(MainAIGoalOut):
    plan: MainAIPlanOut | None
    tasks: list[MainAITaskOut]


class MainAIRecoveryRecordOut(BaseModel):
    """V0.2 minimal API surface -- read-only view of one dead-agent recovery attempt.
    Deliberately mirrors MainAITaskDetailOut's own shape/discipline: every field here is a
    durable column already written by the real recovery pipeline (recovery_inspector.py/
    recovery_classifier.py/recovery_takeover.py), never a value computed just for this
    response."""

    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    task_id: uuid.UUID
    job_id: uuid.UUID
    detected_at: datetime
    classification: str | None = None
    evidence: dict
    salvage_action: str | None = None
    takeover_executor: str | None = None
    takeover_job_id: uuid.UUID | None = None
    status: str
    blocker: str | None = None
    manual_review_required: bool
    completed_at: datetime | None = None
    created_at: datetime


class MainAITaskWaitOut(BaseModel):
    """V0.3 minimal API surface -- read-only view of one durable external-wait record
    (app/models/mainai_wait.py's MainAITaskWait). Same discipline as MainAIRecoveryRecordOut
    above: every field is a durable column already written by the real wait pipeline
    (app/mainai_execution/ci_wait.py), never a value computed just for this response."""

    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    task_id: uuid.UUID
    job_id: uuid.UUID
    source_type: str
    status: str
    resource_ref: dict
    poll_count: int
    last_polled_at: datetime | None = None
    next_poll_at: datetime
    deadline_at: datetime
    evidence: dict
    created_at: datetime
    resolved_at: datetime | None = None


class WorkspaceSupervisedTaskOut(BaseModel):
    """One real MainAI task associated with a workspace, with its durable evidence.

    This is a read composition over the existing execution tables. It never creates or
    executes work, and it never widens the founder-only owner boundary.
    """

    goal: MainAIGoalOut
    task: MainAITaskOut
    events: list[MainAITaskEventOut] = Field(default_factory=list)
    checkpoints: list[MainAICheckpointOut] = Field(default_factory=list)
    waits: list[MainAITaskWaitOut] = Field(default_factory=list)
    recovery_records: list[MainAIRecoveryRecordOut] = Field(default_factory=list)
    job: MainAIJobOut | None = None
    approval_granted: bool = False
    verification_evidence: list[dict] = Field(default_factory=list)


class WorkspaceSupervisedOut(BaseModel):
    goals: list[MainAIGoalOut] = Field(default_factory=list)
    tasks: list[WorkspaceSupervisedTaskOut] = Field(default_factory=list)


class EngineeringLessonOut(BaseModel):
    """V0.3 minimal API surface -- read-only view of one founder-wide engineering lesson
    (app/models/mainai_execution.py's EngineeringLesson), including its `disputed` status once
    app/mainai_execution/lesson_conflicts.py's mark_conflict() has run. Not RLS-protected /
    not owner-scoped -- same reasoning as the model itself."""

    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    status: str
    problem: str
    root_cause: str
    affected_component: str
    severity: str
    evidence: str
    fix: str
    regression_test: str | None = None
    general_rule: str
    applies_to: list
    source_type: str
    source_ref: str
    confidence: str
    created_by: str
    created_at: datetime
    superseded_by: uuid.UUID | None = None


# --- Project Entities / Interpretation Queue / Work Candidates (migration 0054/0055/0056) --
#
# Founder-only API surface making the two governed manual edges
# (interpretation_proposal -> ProjectEntity, WorkCandidate -> MainAIGoal) production
# reachable. See app/routers/project_entities.py's own module docstring: owner_id/authority/
# authorized_by are ALWAYS server-derived from the authenticated founder (Depends(require_
# founder)), never accepted from the request body -- a client cannot spoof identity/authority
# through these schemas even if it tried.


class InterpretationProposalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    source_claim_id: uuid.UUID
    proposed_entity_type: str
    classifier_strategy: str
    classifier_confidence: str
    classifier_reasoning: str | None = None
    status: str
    promoted_to_entity_id: uuid.UUID | None = None
    dismissed_reason: str | None = None
    observed_at: datetime
    created_at: datetime


class PromoteInterpretationProposalIn(BaseModel):
    entity_type: str
    title: str
    summary: str | None = None
    confidence: float | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    supersedes_entity_id: uuid.UUID | None = None


class DismissInterpretationProposalIn(BaseModel):
    reason: str


class ProjectEntityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    entity_type: str
    title: str
    summary: str | None = None
    status: str
    derived_from_claim_id: uuid.UUID
    decided_by: str | None = None
    decided_at: datetime | None = None
    supersedes_entity_id: uuid.UUID | None = None
    authority: str
    basis: str
    confidence: float | None = None
    last_reviewed_at: datetime | None = None
    created_at: datetime


class WorkCandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    source_entity_id: uuid.UUID
    title: str
    rationale: str | None = None
    dependencies: list
    priority: str
    status: str
    authorized_goal_id: uuid.UUID | None = None
    dismissed_reason: str | None = None
    classifier_confidence: float | None = None
    observed_at: datetime
    created_at: datetime


class AuthorizeWorkCandidateIn(BaseModel):
    title: str | None = None
    risk_level: str = "low"
    approval_policy: str = "standard_repo_work"


class DismissWorkCandidateIn(BaseModel):
    reason: str


# --- Execution Authorization Envelope (migration 0057) --------------------------------------
#
# Founder-only API surface making the proposed-scope -> authorized-envelope edge production
# reachable. owner_id/authorized_by are ALWAYS server-derived from the authenticated founder
# (Depends(require_founder)), never accepted from the request body -- same discipline
# app/routers/project_entities.py's own schemas already establish.


class ExecutionScopeProposalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    goal_id: uuid.UUID
    repository_identity: str | None = None
    proposed_paths: list
    proposed_capabilities: list
    proposed_risk: str
    proposal_reasoning: str | None = None
    proposal_strategy: str
    status: str
    authorized_envelope_id: uuid.UUID | None = None
    rejected_reason: str | None = None
    observed_at: datetime
    created_at: datetime


class AuthorizeExecutionScopeIn(BaseModel):
    authorized_paths: list[str]
    authorized_capabilities: list[str]
    authorized_risk: str


class RejectExecutionScopeIn(BaseModel):
    reason: str


class ProposeExecutionScopeIn(BaseModel):
    """Path B bridge input -- the founder's OWN stated suggestion for a directly-created
    goal's execution scope. Carries zero authority (see propose_execution_scope()'s own
    docstring: the proposal is never copied into an envelope automatically); every field here
    defaults to the same honest-empty/low-risk defaults propose_execution_scope() itself
    uses, since this route has no more reliable a signal for "what paths does this goal need"
    than the automatic Path A trigger does."""

    proposed_paths: list[str] = []
    proposed_capabilities: list[str] = []
    proposed_risk: str = "low"
    repository_identity: str | None = None


class ExecutionAuthorizationEnvelopeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    goal_id: uuid.UUID
    source_proposal_id: uuid.UUID | None = None
    repository_identity: str | None = None
    authorized_paths: list
    authorized_capabilities: list
    authorized_risk: str
    authorized_by: str
    authorized_at: datetime
    status: str
    supersedes_envelope_id: uuid.UUID | None = None
    created_at: datetime


class CreateCalendarEventIn(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    starts_at: datetime
    ends_at: datetime | None = None
    description: str | None = Field(default=None, max_length=20_000)
    location: str | None = Field(default=None, max_length=256)
    category: str = "personal"
    timezone: str = "UTC"
    recurrence_rule: str = "none"
    recurrence_until: datetime | None = None
    project_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    milestone_id: uuid.UUID | None = None

    @field_validator("title")
    @classmethod
    def clean_calendar_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Händelsens titel får inte vara tom.")
        return value

    @field_validator("category")
    @classmethod
    def valid_calendar_category(cls, value: str) -> str:
        if value not in {"personal", "work", "school"}:
            raise ValueError("category måste vara personal, work eller school.")
        return value

    @field_validator("recurrence_rule")
    @classmethod
    def valid_recurrence_rule(cls, value: str) -> str:
        if value not in {"none", "daily", "weekly", "monthly"}:
            raise ValueError("recurrence_rule måste vara none, daily, weekly eller monthly.")
        return value


class UpdateCalendarEventIn(BaseModel):
    expected_version: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=256)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    description: str | None = Field(default=None, max_length=20_000)
    location: str | None = Field(default=None, max_length=256)
    category: str | None = None
    timezone: str | None = None
    project_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    milestone_id: uuid.UUID | None = None
    apply_to_series: bool = False

    @field_validator("category")
    @classmethod
    def valid_optional_calendar_category(cls, value: str | None) -> str | None:
        if value is not None and value not in {"personal", "work", "school"}:
            raise ValueError("category måste vara personal, work eller school.")
        return value


class CancelCalendarEventIn(BaseModel):
    reason: str | None = None


class CalendarEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    description: str | None = None
    location: str | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    category: str = "personal"
    timezone: str = "UTC"
    recurrence_rule: str = "none"
    recurrence_until: datetime | None = None
    series_id: uuid.UUID | None = None
    recurrence_index: int = 0
    project_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    milestone_id: uuid.UUID | None = None
    status: str
    source: str
    version: int = 1
    cancelled_at: datetime | None = None
    cancellation_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class DashboardCountsOut(BaseModel):
    active_projects: int = 0
    open_tasks: int = 0
    blocked_tasks: int = 0
    upcoming_events: int = 0
    recent_decisions: int = 0
    recent_documents: int = 0
    active_conversations: int = 0
    active_mainai_goals: int = 0
    running_mainai_tasks: int = 0
    awaiting_approval_tasks: int = 0


class DashboardOut(BaseModel):
    generated_at: datetime
    counts: DashboardCountsOut
    active_projects: list[WorkspaceOut] = Field(default_factory=list)
    outstanding_tasks: list[TaskOut] = Field(default_factory=list)
    blocked_tasks: list[TaskOut] = Field(default_factory=list)
    upcoming_events: list[CalendarEventOut] = Field(default_factory=list)
    recent_decisions: list[WorkspaceDecisionOut] = Field(default_factory=list)
    recent_documents: list[KnowledgeSourceOut] = Field(default_factory=list)
    active_conversations: list[ConversationOut] = Field(default_factory=list)
    supervised_goals: list[MainAIGoalOut] = Field(default_factory=list)
    supervised_tasks: list[MainAITaskOut] = Field(default_factory=list)


class SearchResultOut(BaseModel):
    type: Literal["project", "task", "document", "conversation", "calendar_event"]
    id: uuid.UUID
    title: str
    snippet: str
    project_id: uuid.UUID | None = None
    occurred_at: datetime | None = None
    updated_at: datetime | None = None
    href: str


class SearchResponseOut(BaseModel):
    query: str
    results: list[SearchResultOut] = Field(default_factory=list)
    total: int = 0
    limit: int = 20
    offset: int = 0
    types: list[str] = Field(default_factory=list)
