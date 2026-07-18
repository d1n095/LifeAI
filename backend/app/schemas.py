import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class LoginIn(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, v: str) -> str:
        if not EMAIL_PATTERN.match(v):
            raise ValueError("Ogiltig e-postadress.")
        return v.lower()

    @field_validator("password")
    @classmethod
    def valid_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Lösenordet måste vara minst 8 tecken.")
        return v


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    role: str


class SessionOut(UserOut):
    """Returned only by login/refresh — the one time the CSRF value is transmitted at all.
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
