"""Examiner-owned adapter between the reusable Grandmaster verification suite and a builder SHA.

The suite asserts SECURITY PROPERTIES, not a particular API. When a new builder SHA changes the
API (e.g. an injected clock parameter or a durable lease store), the examiner updates only this
file — never the builder's code — and records the change in README.md for that examination.

Every adapter hook FAILS CLOSED: if the property cannot be exercised on the target SHA, it raises
PropertyNotExposed, which the tests turn into a FAILURE ("P1-n OPEN"), never a skip or a pass.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from typing import Callable

from app.mainai_grandmaster.kernel import GrandmasterKernel
from app.mainai_grandmaster.planner import GrandmasterPlanner


class PropertyNotExposed(AssertionError):
    pass


_CLOCK_KWARGS = ("clock", "now_fn", "time_source", "utc_now")


def kernel_accepts_clock() -> str | None:
    params = inspect.signature(GrandmasterKernel.__init__).parameters
    return next((name for name in _CLOCK_KWARGS if name in params), None)


def make_kernel(board, *, clock: Callable[[], datetime] | None = None) -> GrandmasterKernel:
    """Single in-process kernel. If a clock is supplied it MUST be honoured (P1-1)."""
    if clock is None:
        return GrandmasterKernel(board)
    name = kernel_accepts_clock()
    if name is None:
        raise PropertyNotExposed("P1-1 OPEN: GrandmasterKernel has no injectable clock parameter "
                                 f"(looked for {_CLOCK_KWARGS}); kernel methods read the wall clock")
    return GrandmasterKernel(board, **{name: clock})


def make_planner(kernel) -> GrandmasterPlanner:
    return GrandmasterPlanner(kernel)


# ---------------------------------------------------------------- durable leases (P1-8)
def make_kernels_sharing_durable_lease_store(board_factory, *, clock):
    """Return (kernel_a, kernel_b) that model two worker PROCESSES sharing the production lease store.

    Update for each submission: wire both kernels to the builder's durable store (e.g. a DB session
    factory or lease repository). Until a durable store exists, this fails closed.
    """
    params = inspect.signature(GrandmasterKernel.__init__).parameters
    for name in ("lease_store", "leases", "lease_repository", "store", "session_factory"):
        if name in params:
            raise PropertyNotExposed(
                f"P1-8 ADAPTER UPDATE NEEDED: kernel exposes `{name}`; examiner must wire a shared durable "
                "store here before this property can be judged")
    raise PropertyNotExposed("P1-8 OPEN: no durable/shared lease store exposed; leases are process memory only")


def fencing_token(lease) -> object:
    for name in ("fencing_token", "generation", "lease_generation", "epoch", "token"):
        if hasattr(lease, name):
            return getattr(lease, name)
    raise PropertyNotExposed("P1-8 OPEN: ResourceLease exposes no fencing token/generation")
