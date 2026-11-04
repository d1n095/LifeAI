from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class BootStatus(str, Enum):
    BOOTING = "BOOTING"
    READY = "READY"
    LIMITED = "LIMITED"
    BLOCKED = "BLOCKED"
    FAULT = "FAULT"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    STOPPED = "STOPPED"


class PresenceState(str, Enum):
    OFFLINE = "OFFLINE"
    BOOTING = "BOOTING"
    READY = "READY"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    WORKING = "WORKING"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    NEEDS_FOUNDER = "NEEDS_FOUNDER"
    FAULT = "FAULT"
    SHUTTING_DOWN = "SHUTTING_DOWN"


class RecallBootStatus(str, Enum):
    READY = "READY"
    LIMITED = "LIMITED"
    DISABLED_BY_SECURITY_GATE = "DISABLED_BY_SECURITY_GATE"


class ReadinessState(str, Enum):
    READY = "READY"
    LIMITED = "LIMITED"
    BLOCKED = "BLOCKED"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class ReadinessRecord:
    required: bool
    present: bool
    implemented: bool
    tested: bool
    independently_verified: bool
    integrated: bool
    activated: bool
    safe_for_founder_boot: bool
    blocker: str | None
    evidence: tuple[str, ...]
    exact_sha: str | None
    last_verified: str | None

    def state(self) -> ReadinessState:
        if self.blocker:
            return ReadinessState.BLOCKED if self.required else ReadinessState.DISABLED
        if self.safe_for_founder_boot and self.present and self.implemented and (self.tested or not self.required):
            return ReadinessState.READY
        if self.present and self.implemented:
            return ReadinessState.LIMITED
        return ReadinessState.DISABLED

    def as_dict(self) -> dict[str, Any]:
        return {
            "REQUIRED": self.required,
            "PRESENT": self.present,
            "IMPLEMENTED": self.implemented,
            "TESTED": self.tested,
            "INDEPENDENTLY_VERIFIED": self.independently_verified,
            "INTEGRATED": self.integrated,
            "ACTIVATED": self.activated,
            "SAFE_FOR_FOUNDER_BOOT": self.safe_for_founder_boot,
            "BLOCKER": self.blocker,
            "EVIDENCE": list(self.evidence),
            "EXACT_SHA": self.exact_sha,
            "LAST_VERIFIED": self.last_verified,
            "STATE": self.state().value,
        }
