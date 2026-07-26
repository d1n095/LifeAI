import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

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
    message: str

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


class ChatMessageOut(BaseModel):
    conversation_id: uuid.UUID
    reply: str
    provider: str
    model: str
    sources: list[SourceRef] = []
    confidence: str  # "high" | "medium" | "low" | "none"
    confidence_score: float
    providers_attempted: list[str] = []  # >1 entry means fallback engaged
    conflicts_detected: bool = False
    context_intent: str | None = None  # see app/context/resolver.py — observational only tonight
    context_confidence: str | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    role: str
    content: str
    provider: str | None
    model: str | None
    created_at: datetime


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
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


class TaskIn(BaseModel):
    project_id: uuid.UUID | None = None
    title: str
    description: str | None = None
    status: str = "todo"
    priority: str = "medium"


class TaskOut(TaskIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    suggested_by_ai: bool
    created_at: datetime


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


class ProviderConfigOut(ProviderConfigIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    is_active: bool


class ProviderVerificationOut(BaseModel):
    """P1: the latest real verification result for one (provider, role) pair — never just
    whether a key string is present. `None` (the field is omitted on the Python side, not
    this model) means "never verified yet", surfaced by the frontend as a neutral, not red,
    state."""

    result: str  # ok | invalid_key | unreachable | rate_limited | unsupported | not_configured
    message: str
    checked_by: str  # "system" | "founder"
    checked_at: datetime


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


class KnowledgeClaimOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    claim_text: str
    status: str
    confidence: str  # computed via app/rag/trust.py's assess_claim_confidence(), not the stored extraction-time value
    grounding_score: float
    chunk_id: uuid.UUID | None
    created_at: datetime


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
    failure_reason: str | None
    manifest: dict | None
    file_results: list | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    attempt_count: int
    max_attempts: int
    last_failure_transient: bool | None


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


_NOTE_KINDS = ("fact", "decision", "blocker", "next_step", "uncertainty")
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
