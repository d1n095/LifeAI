"""Examiner-owned adapter between the reusable Grandmaster verification suite and a builder SHA.

The suite asserts SECURITY PROPERTIES, not a particular API. When a new builder SHA changes the
API (e.g. an injected clock parameter or a durable lease store), the examiner updates only this
file — never the builder's code — and records the change in README.md for that examination.

Every adapter hook FAILS CLOSED: if the property cannot be exercised on the target SHA, it raises
PropertyNotExposed, which the tests turn into a FAILURE ("P1-n OPEN"), never a skip or a pass.
"""

from __future__ import annotations

import inspect
import os
import tempfile
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


class _ClockObj:
    """6e08430 takes a Clock object with .now(); the suite's clocks are callables."""
    def __init__(self, fn):
        self.fn = fn

    def now(self):
        return self.fn()


def new_store(path: str | None = None):
    """6e08430: durable SqliteCoordinationStore. Each kernel gets a fresh file unless a shared path is given."""
    from app.mainai_grandmaster.coordination import SqliteCoordinationStore
    if path is None:
        path = os.path.join(tempfile.mkdtemp(prefix="gm-exam-"), "coord.sqlite3")
    return SqliteCoordinationStore(path)


def make_kernel(board, *, clock: Callable[[], datetime] | None = None, store=..., ) -> GrandmasterKernel:
    """Single in-process kernel wired to a durable store (production configuration).

    If `clock` is None the kernel still gets the suite's FIXED clock (fixtures are anchored to 2031), EXCEPT
    that P1-1 passes an explicit clock and moves the wall clock. store=None builds a kernel WITHOUT a store
    (fail-closed checks)."""
    from conftest import FIXED_NOW
    name = kernel_accepts_clock()
    if name is None:
        raise PropertyNotExposed("P1-1 OPEN: GrandmasterKernel has no injectable clock parameter "
                                 f"(looked for {_CLOCK_KWARGS}); kernel methods read the wall clock")
    fn = clock if clock is not None else (lambda: FIXED_NOW)
    return GrandmasterKernel(board, **{name: _ClockObj(fn)}, coordination=new_store() if store is ... else store)


def registry_evidence(kernel, examination_id: str, verdict: str, *, source="verification_registry", examiner=None):
    """Evidence pointer in the shape 6e08430 binds to: payload {examination_id, candidate_sha, examiner_agent, verdict}."""
    from datetime import timedelta
    from app.mainai_grandmaster.types import EvidencePointer
    exam = kernel.board.examinations[examination_id]
    at = kernel.clock.now()
    return EvidencePointer(source, f"{source}:ref", at, True, True, at + timedelta(hours=1),
                           payload={"examination_id": examination_id, "candidate_sha": exam.candidate_sha,
                                    "examiner_agent": examiner or exam.examiner_agent, "verdict": verdict})


def make_planner(kernel) -> GrandmasterPlanner:
    return GrandmasterPlanner(kernel)


# ---------------------------------------------------------------- durable leases (P1-8)
def make_kernels_sharing_durable_lease_store(board_factory, *, clock):
    """Return (kernel_a, kernel_b) that model two worker PROCESSES sharing the production lease store.

    Update for each submission: wire both kernels to the builder's durable store (e.g. a DB session
    factory or lease repository). Until a durable store exists, this fails closed.
    """
    params = inspect.signature(GrandmasterKernel.__init__).parameters
    if "coordination" not in params:
        raise PropertyNotExposed("P1-8 OPEN: no durable/shared lease store exposed; leases are process memory only")
    path = os.path.join(tempfile.mkdtemp(prefix="gm-exam-shared-"), "coord.sqlite3")
    # two SEPARATE store objects on one file = two processes sharing the host authority
    return (make_kernel(board_factory(), clock=clock, store=new_store(path)),
            make_kernel(board_factory(), clock=clock, store=new_store(path)))


def fencing_token(lease) -> object:
    for name in ("fencing_token", "generation", "lease_generation", "epoch", "token"):
        if hasattr(lease, name):
            return getattr(lease, name)
    raise PropertyNotExposed("P1-8 OPEN: ResourceLease exposes no fencing token/generation")
