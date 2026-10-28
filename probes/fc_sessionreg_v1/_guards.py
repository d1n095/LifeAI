"""Minimal network-egress guard for the #315 HTTP integration suite.

The routes are driven in-process via FastAPI TestClient; the only sockets a legitimate run
opens are loopback (Postgres on 127.0.0.1:5433, Redis on localhost). Any connect to a
non-loopback host is refused and recorded, so no test can make a real external call.
Autouse: every test in this suite runs under the guard without opting in.
"""

from __future__ import annotations

import socket

import pytest

LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


@pytest.fixture(autouse=True)
def _no_egress(monkeypatch):
    seen: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(sock, address):
        if sock.family == getattr(socket, "AF_UNIX", None):
            return
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in LOOPBACK:
            seen.append(str(host))
            raise OSError(f"examiner egress guard: connect to {host} refused")

    def connect(self, address):
        _check(self, address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _check(self, address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield seen
