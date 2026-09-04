from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from app.personal_recall.types import DecisionState, IndexState, PersonalKnowledgeItem, Provenance, SourceType, VerificationState


class LocalRecallIndex:
    """Explicit local JSON snapshot. Raw content is optional and never sent anywhere."""

    FORMAT_VERSION = 1

    def __init__(self) -> None:
        self.items: dict[str, PersonalKnowledgeItem] = {}

    def replace(self, items: list[PersonalKnowledgeItem]) -> None:
        self.items = {item.item_id: item for item in items}

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"format_version": self.FORMAT_VERSION, "items": [_encode(i) for i in self.items.values()]}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "LocalRecallIndex":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("format_version") != cls.FORMAT_VERSION:
            raise ValueError("unsupported recall index format")
        index = cls()
        index.replace([_decode(row) for row in payload["items"]])
        return index


def _encode(item: PersonalKnowledgeItem) -> dict:
    row = asdict(item)
    for key in ("source_type", "decision_state", "verification_state", "index_state"):
        row[key] = row[key].value
    row["provenance"]["source_type"] = row["provenance"]["source_type"].value
    for key in ("created_at", "updated_at", "valid_from", "valid_until"):
        row[key] = row[key].isoformat() if row[key] else None
    row["provenance"]["occurred_at"] = item.provenance.occurred_at.isoformat() if item.provenance.occurred_at else None
    return row


def _decode(row: dict) -> PersonalKnowledgeItem:
    data = dict(row)
    p = dict(data.pop("provenance"))
    p["source_type"] = SourceType(p["source_type"])
    p["occurred_at"] = datetime.fromisoformat(p["occurred_at"]) if p.get("occurred_at") else None
    data["provenance"] = Provenance(**p)
    data["source_type"] = SourceType(data["source_type"])
    data["decision_state"] = DecisionState(data["decision_state"])
    data["verification_state"] = VerificationState(data["verification_state"])
    data["index_state"] = IndexState(data["index_state"])
    for key in ("created_at", "updated_at", "valid_from", "valid_until"):
        data[key] = datetime.fromisoformat(data[key]) if data.get(key) else None
    data["entities"] = tuple(data.get("entities", ()))
    data["claims"] = tuple(data.get("claims", ()))
    data["aliases"] = tuple(data.get("aliases", ()))
    data["relationship_edges"] = {k: tuple(v) for k, v in data.get("relationship_edges", {}).items()}
    return PersonalKnowledgeItem(**data)
