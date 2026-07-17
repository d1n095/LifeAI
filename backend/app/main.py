from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import Base, engine
from app.routers import admin, chat, documents, health, knowledge, projects

settings = get_settings()

app = FastAPI(title="LifeOS / MainAI API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(chat.router)
app.include_router(documents.router)
app.include_router(projects.router)
app.include_router(knowledge.router)
app.include_router(admin.router)


@app.on_event("startup")
def on_startup():
    # MVP: create tables directly. Replace with Alembic migrations before production (see ROADMAP Fas 1).
    Base.metadata.create_all(bind=engine)
