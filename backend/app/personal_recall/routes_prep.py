"""Unregistered HTTP factory. Nothing imports this module from app.main.

Grant/session binding is supplied by trusted server dependencies, never a request body.
Only the explicitly disclosure-filtered handoff crosses HTTP; raw RecallResponse does not.
"""
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from app.personal_recall.authorization import RecallAuthorizationError
from app.personal_recall.service import RecallContextHints, RecallMode, RecallQueryRequest, RecallStatusRequest


class QueryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=4_000)
    mode: RecallMode = RecallMode.BROAD
    active_project_id: str | None = Field(default=None, max_length=256)
    current_conversation_id: str | None = Field(default=None, max_length=256)
    active_intent_id: str | None = Field(default=None, max_length=256)
    workspace_file_id: str | None = Field(default=None, max_length=256)


def build_recall_router(*, service_dependency, authorization_dependency, authorization_gate, enabled=False):
    router = APIRouter(prefix="/personal-recall", tags=["personal-recall-prep"])

    def gate():
        if not enabled or authorization_gate() is not True:
            raise HTTPException(status_code=404, detail="Not found")

    @router.post("/query", dependencies=[Depends(gate)])
    def query(body: QueryBody, service=Depends(service_dependency), authority=Depends(authorization_dependency)):
        try:
            hints = RecallContextHints(body.current_conversation_id, body.active_project_id, body.active_intent_id, body.workspace_file_id)
            response = service.query(RecallQueryRequest(body.query, authority, hints, body.mode))
            return jsonable_encoder({"contract_version": "personal-recall-evidence-v1",
                                     "handoff": asdict(response.handoff),
                                     "authorization_receipt": asdict(response.authorization_receipt)})
        except RecallAuthorizationError:
            raise HTTPException(status_code=403, detail="Recall authority denied") from None

    @router.get("/status", dependencies=[Depends(gate)])
    def status(service=Depends(service_dependency), authority=Depends(authorization_dependency)):
        try:
            return service.status(RecallStatusRequest(authority))
        except RecallAuthorizationError:
            raise HTTPException(status_code=403, detail="Recall authority denied") from None

    return router
