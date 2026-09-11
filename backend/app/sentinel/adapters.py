"""Sentinel -- future event source adapter interfaces (Stage V2-D2, Security Event Mesh).

Typed Protocol stubs only. No concrete adapter exists yet, and none of these performs any
real OS-level monitoring, file access, or network call -- they exist purely to prove the
shape a future adapter must implement before it can feed events into
app.sentinel.service.record_event(). See docs/mainai_v2/MAINAI_V2_IMPLEMENTATION_PLAN.md's
event-source map for what wiring each of these would eventually require, and why none of it
happens yet.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.sentinel.types import SecurityEvent


@runtime_checkable
class SecurityEventAdapter(Protocol):
    """Base interface every future event source implements. `poll()` returns already-
    normalized SecurityEvent objects -- an adapter is responsible for its own
    classification/hashing before Sentinel ever sees the data; Sentinel itself never reaches
    out to collect raw signal."""

    adapter_name: str
    adapter_version: str

    def poll(self) -> list[SecurityEvent]: ...


class FileScannerAdapter(SecurityEventAdapter, Protocol):
    """Future source for BINARY_CHANGED, MASS_FILE_READ, MASS_FILE_WRITE, RANSOMWARE_PATTERN,
    UNTRUSTED_FILE_OPENED."""


class NetworkMonitorAdapter(SecurityEventAdapter, Protocol):
    """Future source for UNEXPECTED_EGRESS, NEW_OUTBOUND_DESTINATION, SUSPICIOUS_DNS."""


class ProcessMonitorAdapter(SecurityEventAdapter, Protocol):
    """Future source for PROCESS_STARTED, PROCESS_INJECTION_ATTEMPT,
    UNSIGNED_BINARY_EXECUTED, PRIVILEGE_ESCALATION_ATTEMPT, SCRIPT_EXECUTION."""


class BrowserIsolationAdapter(SecurityEventAdapter, Protocol):
    """Future source for BROWSER_EXPLOIT_SIGNAL."""


class USBMonitorAdapter(SecurityEventAdapter, Protocol):
    """Future source for USB_CONNECTED, USB_HID_BEHAVIOR."""


class BluetoothWifiMonitorAdapter(SecurityEventAdapter, Protocol):
    """Future source for BLUETOOTH_PAIR_ATTEMPT, WIFI_NETWORK_CHANGE."""


class VaultAdapter(SecurityEventAdapter, Protocol):
    """Future source for VAULT_ACCESS_ATTEMPT, CREDENTIAL_READ_ATTEMPT."""


class GuardianAdapter(SecurityEventAdapter, Protocol):
    """Future source for DEVICE_TRUST_CHANGED, RECOVERY_TRIGGER, BOOT_INTEGRITY_FAILURE --
    Guardian state transitions Sentinel should be aware of as *observations*, never as
    something this package reaches into app.guardian to read directly (see module docstring
    in types.py: Sentinel does not import app.guardian)."""


class AgentRuntimeAdapter(SecurityEventAdapter, Protocol):
    """Future source for AGENT_SCOPE_ESCALATION, PRIVILEGE_ESCALATION_ATTEMPT."""


class ModelPluginLoaderAdapter(SecurityEventAdapter, Protocol):
    """Future source for MODEL_CHANGED, PLUGIN_CHANGED."""


class RecoverySubsystemAdapter(SecurityEventAdapter, Protocol):
    """Future source for RECOVERY_TRIGGER, SECURITY_SETTING_CHANGED, POLICY_CHANGE."""
