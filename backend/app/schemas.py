import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ChatMessageIn(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str


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
