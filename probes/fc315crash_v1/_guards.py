"""Network-egress guard for the #315 crash-consistency suite.

These tests are pure local filesystem (ResumableHttpStore + fault injection) and must open no
sockets at all. Any connect to a non-loopback host is refused and recorded. Autouse.
"""

from __future__ import annotations

import socket

import pytest

LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


@pytest.fixture(autouse=True)
def _no_egress(monkeypatch):
    seen: list[str] = []
    real_connect = socket.socket.connect

    def connect(self, address):
        if self.family != getattr(socket, "AF_UNIX", None):
            host = address[0] if isinstance(address, tuple) else str(address)
            if host not in LOOPBACK:
                seen.append(str(host))
                raise OSError(f"examiner egress guard: connect to {host} refused")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    yield seen
