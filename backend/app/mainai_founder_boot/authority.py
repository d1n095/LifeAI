from __future__ import annotations

from dataclasses import dataclass

COMPUTER_CONTROL_CAPABILITIES = {
    "READ_SCREEN", "READ_APP_CONTEXT", "READ_FILES", "WRITE_FILES", "CONTROL_WINDOWS",
    "MOUSE_INPUT", "KEYBOARD_INPUT", "BROWSER_NAVIGATION", "FORM_FILL", "TERMINAL_READ", "TERMINAL_EXECUTE",
    "SEND_MESSAGE", "SUBMIT_FORM", "DELETE_DATA", "PURCHASE", "PUSH_GIT", "MERGE", "DEPLOY", "FINANCIAL_TRANSFER",
}

CAPABILITY_INVARIANTS = (
    "CAN SEE != CAN CONTROL",
    "CAN CONTROL WINDOW != CAN TYPE",
    "CAN TYPE != CAN SUBMIT",
    "CAN READ FILE != CAN WRITE FILE",
    "CAN MODIFY CODE != CAN PUSH",
    "CAN PUSH != CAN MERGE",
    "CAN MERGE != CAN DEPLOY",
    "CAN NAVIGATE FINANCE UI != CAN TRANSFER MONEY",
)

DEFAULT_FOUNDER_BOOT_AUTHORITY_PROFILE = {
    "mode": "FOUNDER_ONLY",
    "granted_computer_control": [],
    "forbidden_effects": ["MERGE", "DEPLOY", "FINANCIAL_TRANSFER", "PURCHASE", "PUSH_GIT", "TERMINAL_EXECUTE"],
    "remote_writes_enabled": False,
    "unrestricted_providers_enabled": False,
    "merge_enabled": False,
    "deploy_enabled": False,
}


@dataclass(frozen=True)
class CapabilityDecision:
    allowed: bool
    reason: str


def require_capability(profile: dict, capability: str) -> CapabilityDecision:
    if capability not in COMPUTER_CONTROL_CAPABILITIES:
        return CapabilityDecision(False, "unknown capability")
    if capability in set(profile.get("granted_computer_control", [])):
        return CapabilityDecision(True, "explicitly granted")
    return CapabilityDecision(False, "capability not granted for founder boot")


def reject_self_grant(capability: str) -> None:
    raise PermissionError(f"MainAI cannot self-grant capability {capability}")
