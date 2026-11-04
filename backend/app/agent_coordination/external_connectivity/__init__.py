"""Real External Agent Connectivity — inspection and fail-closed first path.

Reuses the #302/#303 gateway, local authorized bridge and durable ledger.
Does not amend #303–#306 and does not claim vendor delivery.
"""

from app.agent_coordination.external_connectivity.catalog import official_interface_catalog, selected_first_path
from app.agent_coordination.external_connectivity.cursor_cloud import (
    CursorCloudAgentClient,
    probe_unauthenticated_surface,
    verify_v0_webhook,
)
from app.agent_coordination.external_connectivity.identity import (
    IdentityClass,
    classify_credential,
    runtime_identity_observations,
)
from app.agent_coordination.external_connectivity.inspect import inspect_external_connectivity
from app.agent_coordination.external_connectivity.ledger import propose_ledger_event
from app.agent_coordination.external_connectivity.verdict import (
    capability_verdict,
    unattended_trial_plan,
    vertical_slice,
)

__all__ = [
    "CursorCloudAgentClient",
    "IdentityClass",
    "capability_verdict",
    "classify_credential",
    "inspect_external_connectivity",
    "official_interface_catalog",
    "probe_unauthenticated_surface",
    "propose_ledger_event",
    "runtime_identity_observations",
    "selected_first_path",
    "unattended_trial_plan",
    "verify_v0_webhook",
    "vertical_slice",
]
