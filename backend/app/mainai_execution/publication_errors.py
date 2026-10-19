"""Typed, origin-aware failures at the first-cycle push / grant / publication boundary.

Classification for paid-retry / replan suppression MUST use these types (or structured
`origin` / `failure_class` / `first_cycle_state` fields our own code set). Exception
messages and model-controlled `evidence["error"]` prose are never a classification input.

Unknown errors at the push/grant boundary fail closed: no retry, no paid replan.
Codex Git transport / production grants stay unimplemented; `GrantDeniedError` exists so a
future grant path can raise a typed failure without this module inventing a second transport.
"""

from __future__ import annotations

import subprocess
from typing import Any

ORIGIN_PUSH = "push"
ORIGIN_GRANT = "grant"
ORIGIN_APPROVED_BASE = "approved_base"
ORIGIN_GITHUB_API = "github_api"
ORIGIN_NETWORK = "network"
ORIGIN_TIMEOUT = "timeout"
ORIGIN_CI = "ci"
ORIGIN_REVIEW = "review"
ORIGIN_STOP = "stop"
ORIGIN_UNKNOWN = "unknown"

FAIL_CLOSED_ORIGINS = frozenset(
    {
        ORIGIN_PUSH,
        ORIGIN_GRANT,
        ORIGIN_APPROVED_BASE,
        ORIGIN_GITHUB_API,
        ORIGIN_NETWORK,
        ORIGIN_TIMEOUT,
        ORIGIN_CI,
        ORIGIN_REVIEW,
        ORIGIN_STOP,
        ORIGIN_UNKNOWN,
    }
)

STATE_EXTERNAL_BOUNDARY_FAILED = "external_boundary_failed"
STATE_EXTERNAL_PUSH_REJECTED = "external_push_rejected"
STATE_APPROVED_BASE_MOVED = "approved_base_moved"
STATE_GRANT_DENIED_OR_EXPIRED = "grant_denied_or_expired"
STATE_EXTERNAL_CI_FAILED = "external_ci_failed"
STATE_EXTERNAL_CI_TIMED_OUT = "external_ci_timed_out"
STATE_INDEPENDENT_REVIEW_BLOCKED = "independent_review_blocked"
STATE_STOP_BEFORE_MERGE = "stop_before_merge"

ORIGIN_TO_STOP_STATE = {
    ORIGIN_PUSH: STATE_EXTERNAL_PUSH_REJECTED,
    ORIGIN_GRANT: STATE_GRANT_DENIED_OR_EXPIRED,
    ORIGIN_APPROVED_BASE: STATE_APPROVED_BASE_MOVED,
    ORIGIN_GITHUB_API: STATE_EXTERNAL_BOUNDARY_FAILED,
    ORIGIN_NETWORK: STATE_EXTERNAL_BOUNDARY_FAILED,
    ORIGIN_TIMEOUT: STATE_EXTERNAL_BOUNDARY_FAILED,
    ORIGIN_CI: STATE_EXTERNAL_CI_FAILED,
    ORIGIN_REVIEW: STATE_INDEPENDENT_REVIEW_BLOCKED,
    ORIGIN_STOP: STATE_STOP_BEFORE_MERGE,
    ORIGIN_UNKNOWN: STATE_EXTERNAL_BOUNDARY_FAILED,
}


class ExternalBoundaryError(RuntimeError):
    """Fail-closed external publication / grant / GitHub / network / timeout failure."""

    origin: str = ORIGIN_UNKNOWN
    first_cycle_state: str = STATE_EXTERNAL_BOUNDARY_FAILED
    fail_closed: bool = True

    def __init__(
        self,
        message: str = "",
        *,
        origin: str | None = None,
        first_cycle_state: str | None = None,
    ):
        super().__init__(message or type(self).__name__)
        if origin is not None:
            self.origin = origin
        if first_cycle_state is not None:
            self.first_cycle_state = first_cycle_state

    def evidence(self) -> dict[str, Any]:
        """Structured classifier inputs. Never a substitute for reading `str(self)`."""
        return {
            "origin": self.origin,
            "failure_class": type(self).__name__,
            "first_cycle_state": self.first_cycle_state,
            "error": type(self).__name__,
        }


class PushRejectedError(ExternalBoundaryError):
    origin = ORIGIN_PUSH
    first_cycle_state = STATE_EXTERNAL_PUSH_REJECTED


class GrantDeniedError(ExternalBoundaryError):
    """Typed grant denial. Production grant/transport remains Codex FAIL/BLOCKED."""

    origin = ORIGIN_GRANT
    first_cycle_state = STATE_GRANT_DENIED_OR_EXPIRED


class ApprovedBaseMovedError(ExternalBoundaryError):
    origin = ORIGIN_APPROVED_BASE
    first_cycle_state = STATE_APPROVED_BASE_MOVED


class GitHubApiBoundaryError(ExternalBoundaryError):
    origin = ORIGIN_GITHUB_API
    first_cycle_state = STATE_EXTERNAL_BOUNDARY_FAILED

    def __init__(self, message: str = "", *, status_code: int | None = None, **kwargs: Any):
        super().__init__(message, **kwargs)
        self.status_code = status_code


class PublicationTimeoutError(ExternalBoundaryError):
    origin = ORIGIN_TIMEOUT
    first_cycle_state = STATE_EXTERNAL_BOUNDARY_FAILED


class PublicationNetworkError(ExternalBoundaryError):
    origin = ORIGIN_NETWORK
    first_cycle_state = STATE_EXTERNAL_BOUNDARY_FAILED


class PublicationUnknownError(ExternalBoundaryError):
    """Any unclassified error at the push/grant boundary. Fail closed."""

    origin = ORIGIN_UNKNOWN
    first_cycle_state = STATE_EXTERNAL_BOUNDARY_FAILED


class PublicationForbiddenAfterStopError(ExternalBoundaryError):
    """A first-cycle STOP is already durable; push / open_pr / grant must not proceed."""

    origin = ORIGIN_STOP
    first_cycle_state = STATE_STOP_BEFORE_MERGE


class ExternalCiContractError(ExternalBoundaryError):
    origin = ORIGIN_CI
    first_cycle_state = STATE_EXTERNAL_CI_FAILED


def map_push_grant_boundary_error(exc: BaseException) -> ExternalBoundaryError:
    """Map a push/grant-boundary exception to a typed fail-closed error.

    Does not inspect exception message text. Unknown types fail closed.
    """
    if isinstance(exc, ExternalBoundaryError):
        return exc

    type_name = type(exc).__name__
    module_name = getattr(type(exc), "__module__", "") or ""

    from app.integrations.github_client import GitHubClientError
    from app.mainai_execution.worktree import (
        ActivationBaseConfigurationError,
        WorktreePushRejected,
        WorktreeTimeoutError,
    )

    if isinstance(exc, WorktreePushRejected):
        return exc
    if isinstance(exc, ActivationBaseConfigurationError):
        return exc
    if isinstance(exc, GitHubClientError):
        return GitHubApiBoundaryError(type_name, status_code=getattr(exc, "status_code", None))
    if isinstance(exc, WorktreeTimeoutError):
        return PublicationTimeoutError(type_name)
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return PublicationTimeoutError(type_name)
    if type_name in {"TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout"}:
        return PublicationTimeoutError(type_name)
    if isinstance(exc, (ConnectionError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return PublicationNetworkError(type_name)
    if type_name in {
        "NetworkError",
        "ConnectError",
        "ReadError",
        "WriteError",
        "TransportError",
        "ProxyError",
        "ProtocolError",
        "RemoteProtocolError",
        "LocalProtocolError",
        "UnsupportedProtocol",
    } or module_name.startswith("httpx") or module_name.startswith("httpcore"):
        if "Timeout" in type_name:
            return PublicationTimeoutError(type_name)
        return PublicationNetworkError(type_name)
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in {101, 104, 111, 113, 110}:
        return PublicationNetworkError(type_name)
    return PublicationUnknownError(type_name)
