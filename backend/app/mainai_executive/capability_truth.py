"""Read-only truth about what MainAI can actually do right now.

Four facts are routinely conflated, and each one needs its own evidence:

* **implemented** -- the backing module exists in this checkout and is wired through an
  import/router reference. ``SystemIntelligence`` supplies that from the static scan.
* **configured** -- the prerequisite (an API key, a local runtime) is present.
* **available** -- a caller-supplied probe has confirmed it *now*.
* **permitted** -- the existing authority gate would allow this caller to use it.

This module performs no probe, contacts no provider, authorizes no spend and grants no
egress. An unanswered question stays ``None`` (unknown) and is never rounded up to
"available". Vendor claims are not evidence: a model advertising ``tools`` does not mean a
tool-calling transport is wired, so that question is answered from the transport's own
declared capabilities instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Mapping

from sqlalchemy.orm import Session

from app.config import get_settings
from app.providers.catalog import ModelSelectionError, model_specs
from app.providers.registry import resolve_chat_chain

CapabilityKind = Literal["model_tool", "platform_action", "read_only"]
SideEffect = Literal["none", "external", "writes"]
CircuitState = Literal["open", "closed", "unknown"]


@dataclass(frozen=True)
class CapabilityTruth:
    """One capability, with each of the four facts kept separate."""

    id: str
    label: str
    module: str
    kind: CapabilityKind
    side_effect: SideEffect
    requires: tuple[str, ...]
    gate: str
    wired: bool | None = None
    available: bool | None = None
    permitted: bool | None = None
    note: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "module": self.module,
            "kind": self.kind,
            "side_effect": self.side_effect,
            "requires": list(self.requires),
            "gate": self.gate,
            "wired": self.wired,
            "available": self.available,
            "permitted": self.permitted,
            "note": self.note,
        }


# Only code paths that exist in this repository are declared. A capability that is not
# listed here is not silently assumed to work; callers get no entry at all.
_DECLARED: tuple[CapabilityTruth, ...] = (
    CapabilityTruth(
        id="chat.reply",
        label="Svara i en konversation",
        module="app.routers.chat",
        kind="platform_action",
        side_effect="external",
        requires=("en konfigurerad chattleverantör eller en lokal runtime",),
        gate="egress_policy.enforce_egress_policy + billing.reservation + provider circuit",
        note="ett externt svar kräver owner_id eller en verifierad worker-lease",
    ),
    CapabilityTruth(
        id="chat.stream",
        label="Strömma ett svar",
        module="app.routers.chat",
        kind="platform_action",
        side_effect="external",
        requires=("en konfigurerad chattleverantör eller en lokal runtime",),
        gate="egress_policy.enforce_egress_policy + billing.reservation + provider circuit",
        note="efter första deltat är ett automatiskt leverantörsbyte blockerat",
    ),
    CapabilityTruth(
        id="providers.verify",
        label="Verifiera en leverantör och exakt modell",
        module="app.providers.verification",
        kind="platform_action",
        side_effect="external",
        requires=("leverantörens nyckel",),
        gate="require_founder + provider verification",
    ),
    CapabilityTruth(
        id="providers.local_models",
        label="Lista lokalt installerade modeller",
        module="app.providers.local_discovery",
        kind="read_only",
        side_effect="none",
        requires=("en nåbar lokal runtime",),
        gate="require_founder",
    ),
    CapabilityTruth(
        id="system.assess",
        label="Bedöma systemet och föreslå nästa steg",
        module="app.context_atlas.runtime",
        kind="read_only",
        side_effect="none",
        requires=(),
        gate="ingen (read-only); resultatet utför ingenting",
    ),
    CapabilityTruth(
        id="work.ownership",
        label="Se vem som redan arbetar i en fil",
        module="app.memory_work_linkage.service",
        kind="read_only",
        side_effect="none",
        requires=(),
        gate="owner-scope",
        note="en icke-terminal tilldelning är ett bevis på en tilldelning, inte på en körande process",
    ),
    CapabilityTruth(
        id="dev.pr_handling",
        label="Hantera en PR och köra allowlistade tester",
        module="app.jobs.service",
        kind="platform_action",
        side_effect="writes",
        requires=("en aktiv job-lease",),
        gate="job lease + development_operator allowlist",
    ),
    CapabilityTruth(
        id="github.status",
        label="Läsa GitHub-status",
        module="app.github_status.service",
        kind="read_only",
        side_effect="none",
        requires=("en GitHub-token",),
        gate="owner-scope",
        note="utan token är status okänd, inte grön",
    ),
)


def capability_inventory(
    *,
    wired: Mapping[str, bool | None] | None = None,
    available: Mapping[str, bool | None] | None = None,
    permitted: Mapping[str, bool | None] | None = None,
) -> tuple[CapabilityTruth, ...]:
    """Join the declarations with whatever evidence the caller actually has.

    Every mapping is optional. A missing key means "not answered", which is reported as
    ``None`` -- never as ``False`` (a known failure) and never as ``True`` (a grant).
    """

    wired = wired or {}
    available = available or {}
    permitted = permitted or {}
    return tuple(
        CapabilityTruth(
            id=item.id,
            label=item.label,
            module=item.module,
            kind=item.kind,
            side_effect=item.side_effect,
            requires=item.requires,
            gate=item.gate,
            wired=wired.get(item.id),
            available=available.get(item.id),
            permitted=permitted.get(item.id),
            note=item.note,
        )
        for item in _DECLARED
    )


def model_tool_calling() -> dict:
    """Whether a model can actually call tools through the wired transports.

    The catalog records vendor capability claims; the transports only carry text chat and
    embeddings. Reporting the claim as if it were a wired tool would be the exact
    configured-vs-implemented confusion this module exists to prevent.
    """

    claiming = sorted({spec.provider for spec in model_specs() if "tools" in spec.capabilities})
    wired = sorted({cap for spec in model_specs() for cap in spec.supported_capabilities})
    return {
        "wired": False,
        "vendors_claiming_tools": claiming,
        "wired_capabilities": wired,
        "reason": (
            "Message/ChatResult transports carry text (and embeddings) only; "
            "ModelSpec.supported_capabilities intersects vendor claims down to what is wired"
        ),
    }


@dataclass(frozen=True)
class RouteEntry:
    """One provider/model hop, with every unproven fact left unproven.

    ``circuit_open`` is ``None`` when the breaker store did not answer, and ``usable_now`` is
    only ``True`` with a positive probe -- a closed circuit and a configured key are
    prerequisites, not evidence that the provider answers right now.
    """

    provider: str
    model: str
    external: bool
    configured: bool
    circuit_state: CircuitState
    circuit_open: bool | None
    verification: str | None
    verification_age_seconds: int | None
    verification_stale: bool | None
    usable_now: bool | None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "external": self.external,
            "configured": self.configured,
            "circuit_state": self.circuit_state,
            "circuit_open": self.circuit_open,
            "verification": self.verification,
            "verification_age_seconds": self.verification_age_seconds,
            "verification_stale": self.verification_stale,
            "usable_now": self.usable_now,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ChatRouteTruth:
    """The chain the existing router would actually walk, and what is known about it."""

    status: str
    primary: RouteEntry | None
    entries: tuple[RouteEntry, ...]
    reason: str | None
    limitations: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "primary": self.primary.to_dict() if self.primary is not None else None,
            "entries": [entry.to_dict() for entry in self.entries],
            "reason": self.reason,
            "limitations": list(self.limitations),
        }


def chat_route_truth(
    db: Session,
    *,
    probes: Mapping[str, bool | None] | None = None,
) -> ChatRouteTruth:
    """Report the effective chat chain without contacting any provider.

    Uses ``resolve_chat_chain`` itself rather than rebuilding the ordering, so this view
    cannot drift from the routing the chat router really uses. ``probes`` may confirm that a
    provider answers right now (for example a local runtime health check); without one,
    ``usable_now`` means only "configured and not circuit-open".
    """

    from app.providers import circuit_breaker
    from app.providers.base import ProviderError
    from app.providers.verification import latest_check

    probes = probes or {}
    limitations = [
        "ingen leverantör kontaktas; konfigurerad betyder att en nyckel/runtime finns, inte att modellen svarar",
        "en stängd krets är ett frånvarande stopp, inte ett friskhetstecken; en onåbar breaker-lagring rapporteras som okänd",
        "verifiering är exakt-modell-bevis men krävs inte på den äldre chattvägen, och en gammal rad räknas inte som aktuell",
        "den här vyn utför ingenting och autoriserar ingen utgift eller egress",
    ]
    try:
        chain = resolve_chat_chain(db)
    except (ProviderError, ModelSelectionError) as exc:
        return ChatRouteTruth(
            status="unavailable",
            primary=None,
            entries=(),
            reason=str(exc),
            limitations=tuple(limitations),
        )
    except Exception as exc:  # noqa: BLE001 - a truth view must never take its caller down
        # Never str(exc) here: a driver/connection error can embed the DSN, password included.
        return ChatRouteTruth(
            status="unavailable",
            primary=None,
            entries=(),
            reason=f"chattkedjan kunde inte byggas ({type(exc).__name__})",
            limitations=tuple(limitations),
        )

    now = datetime.utcnow()
    stale_after = get_settings().provider_verification_cache_seconds
    entries: list[RouteEntry] = []
    for provider, model in chain:
        notes: list[str] = []
        try:
            configured = provider.is_configured()
        except Exception:  # noqa: BLE001 - an unreadable config is not a configured provider
            configured = False
            notes.append("konfigurationen kunde inte läsas; behandlas som ej konfigurerad")

        circuit_state: CircuitState = circuit_breaker.state(provider.name, model)  # type: ignore[assignment]
        circuit_open: bool | None = True if circuit_state == "open" else (
            False if circuit_state == "closed" else None
        )
        if circuit_state == "unknown":
            notes.append(
                "breaker-lagringen svarade inte (nere eller timeout); stängd krets kan inte bekräftas"
            )

        verification: str | None = None
        verification_age: int | None = None
        verification_stale: bool | None = None
        try:
            check = latest_check(db, provider.name, "chat", model=model)
        except Exception as exc:  # noqa: BLE001 - a broken health read is unknown, not healthy
            check = None
            notes.append(
                f"hälsokontrollen kunde inte läsas ({type(exc).__name__}); status förblir okänd"
            )
        if check is not None:
            raw_result = getattr(check, "result", None)
            value = getattr(raw_result, "value", None)
            if isinstance(value, str):
                verification = value
            else:
                notes.append("hälsokontrollen hade en okänd resultattyp; den räknas inte som bevis")
            checked_at = getattr(check, "checked_at", None)
            if isinstance(checked_at, datetime):
                verification_age = max(0, int((now - checked_at).total_seconds()))
                verification_stale = verification_age > stale_after
                if verification_stale:
                    notes.append(
                        f"hälsokontrollen är {verification_age} s gammal och räknas inte som aktuell"
                    )
            else:
                notes.append("hälsokontrollen saknar tidsstämpel; färskheten är okänd")

        # A known blocker makes it definitely unusable; otherwise only a probe can say yes.
        usable: bool | None
        if not configured:
            usable = False
        elif circuit_state == "open":
            usable = False
        else:
            usable = None
        probe = probes.get(provider.name)
        if probe is False:
            usable = False
        elif probe is True and configured:
            usable = True

        entries.append(
            RouteEntry(
                provider=provider.name,
                model=model,
                external=not getattr(provider, "is_local", False),
                configured=configured,
                circuit_state=circuit_state,
                circuit_open=circuit_open,
                verification=verification,
                verification_age_seconds=verification_age,
                verification_stale=verification_stale,
                usable_now=usable,
                notes=tuple(notes),
            )
        )

    return ChatRouteTruth(
        status="ok",
        primary=entries[0] if entries else None,
        entries=tuple(entries),
        reason=None if entries else "ingen chattkedja kunde byggas",
        limitations=tuple(limitations),
    )


@dataclass(frozen=True)
class CapabilityView:
    """The joined answer, always non-executing."""

    tools: tuple[CapabilityTruth, ...]
    model_tool_calling: dict
    chat_route: ChatRouteTruth
    limitations: tuple[str, ...]
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        return {
            "tools": [tool.to_dict() for tool in self.tools],
            "model_tool_calling": self.model_tool_calling,
            "chat_route": self.chat_route.to_dict(),
            "limitations": list(self.limitations),
            "execution_authorized": False,
        }
