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


class ChatMessageOut(BaseModel):
    conversation_id: uuid.UUID
    reply: str
    provider: str
    model: str
    sources: list[SourceRef] = []
    confidence: str  # "high" | "medium" | "low" | "none"
    confidence_score: float
    providers_attempted: list[str] = []  # >1 entry means fallback engaged


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


class ProviderStatus(BaseModel):
    name: str
    configured: bool
    active_chat: bool
    active_embedding: bool


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


class KnowledgeSourceDetailOut(KnowledgeSourceOut):
    versions: list[KnowledgeVersionOut] = []
    relationships: list[SourceRelationshipOut] = []
    chunk_preview: list[str] = []


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


class LibrarySearchHit(BaseModel):
    document_id: uuid.UUID
    title: str
    text: str
    score: float
    classification: str
    active_truth_status: str
    media_type: str | None
    text_match: bool = False


class DeleteConfirmIn(BaseModel):
    confirm: bool = False
