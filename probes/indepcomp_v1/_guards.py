"""Network and paid-call guards for the application-readiness suite.

- every LLM provider's chat/embed is replaced by a recorder that raises ProviderError (no paid call
  can complete; every ATTEMPT is counted);
- every non-loopback socket connect is refused and recorded (no egress at all: GitHub, providers,
  registries).
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import socket

import pytest

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class Egress:
    def __init__(self):
        self.provider_calls: list[tuple[str, str]] = []
        self.sockets: list[str] = []


@pytest.fixture
def egress(monkeypatch):
    from app.providers.base import ProviderError

    rec = Egress()
    import app.providers as pkg

    seen = set()
    for info in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f"app.providers.{info.name}")
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if cls in seen or not cls.__module__.startswith("app.providers"):
                continue
            seen.add(cls)
            for meth in ("chat", "embed", "stream_chat", "transcribe"):
                if meth in cls.__dict__ and callable(cls.__dict__[meth]):
                    def make(cname, mname):
                        async def blocked(self, *a, **k):
                            rec.provider_calls.append((cname, mname))
                            raise ProviderError(f"examiner: {cname}.{mname} blocked (would be a paid call)")
                        return blocked
                    monkeypatch.setattr(cls, meth, make(cls.__name__, meth))

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(sock, address):
        if sock.family == getattr(socket, "AF_UNIX", None):
            return
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in LOOPBACK:
            rec.sockets.append(f"{host}:{address[1] if isinstance(address, tuple) and len(address) > 1 else ''}")
            raise OSError(f"examiner egress guard: connect to {host} refused")

    def connect(sock, address):
        _check(sock, address)
        return real_connect(sock, address)

    def connect_ex(sock, address):
        _check(sock, address)
        return real_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    return rec
