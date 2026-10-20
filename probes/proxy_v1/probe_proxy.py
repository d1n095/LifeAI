"""PX-1..PX-8: same-origin proxy (frontend/app/api/[...path]/route.ts) against bypasses and leakage.
Raw sockets keep percent-encoding exactly as an attacker sends it. Usage: probe_proxy.py <next_port>"""
import json, socket, sys

PORT = int(sys.argv[1])
results, bad = [], []


def raw(req: bytes):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=20)
    s.sendall(req)
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = [l.split(":", 1) for l in lines[1:] if ":" in l]
    if any(k.lower() == "transfer-encoding" and "chunked" in v for k, v in headers):
        out, rest = b"", body
        while rest:
            size_line, _, rest = rest.partition(b"\r\n")
            size = int(size_line.split(b";")[0] or b"0", 16)
            if size == 0:
                break
            out, rest = out + rest[:size], rest[size + 2:]
        body = out
    try:
        j = json.loads(body)
    except Exception:
        j = None
    return status, headers, j


def get(path, extra=""):
    return raw(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n{extra}Connection: close\r\n\r\n".encode())


# PX-1 path traversal must never leave the backend's /api/ namespace
for p in ["/api/%2e%2e/openapi.json", "/api/..%2fopenapi.json", "/api/a/%2e%2e/%2e%2e/docs", "/api/%2e%2e%2f%2e%2e%2fdocs",
          "/api/..%5c..%5cdocs", "/api/%252e%252e/docs", "/api/x/..%2F..%2Fhealth-internal", "/api/.%2e/docs"]:
    st, hd, j = get(p)
    fwd = (j or {}).get("raw_path") if isinstance(j, dict) else None
    ok = st >= 400 or (fwd is not None and fwd.startswith("/api/") and "/../" not in fwd)
    if fwd is not None and not fwd.startswith("/api/"):
        ok = False
    results.append(("PX-1", p, st, fwd))
    if not ok:
        bad.append(f"PX-1 {p} -> backend path {fwd!r} (status {st})")

# PX-2 client-supplied X-Forwarded-For / X-Real-IP reach the backend verbatim (rate-limit/audit key spoofing)
st, hd, j = get("/api/health", "X-Forwarded-For: 203.0.113.77\r\nX-Real-IP: 203.0.113.78\r\nForwarded: for=203.0.113.79\r\n")
h = (j or {}).get("headers", {})
results.append(("PX-2", "xff", st, {k: h.get(k) for k in ("x-forwarded-for", "x-real-ip", "forwarded")}))
if "203.0.113.77" in (h.get("x-forwarded-for") or "").split(",")[0]:
    bad.append(f"PX-2 spoofed X-Forwarded-For forwarded as the LEFTMOST client address: {h.get('x-forwarded-for')!r}")

# PX-3 Host / X-Forwarded-Host
st, hd, j = raw(f"GET /api/health HTTP/1.1\r\nHost: evil.example\r\nX-Forwarded-Host: evil.example\r\nConnection: close\r\n\r\n".encode())
h = (j or {}).get("headers", {})
results.append(("PX-3", "host", st, {"host": h.get("host"), "x-forwarded-host": h.get("x-forwarded-host")}))
if h.get("host") and "evil" in h.get("host"):
    bad.append(f"PX-3 attacker Host forwarded to backend: {h.get('host')}")

# PX-4 hop-by-hop / smuggling headers stripped
st, hd, j = get("/api/health", "Proxy-Authorization: Basic Zm9vOmJhcg==\r\nTE: trailers\r\nUpgrade: h2c\r\nConnection: close, X-Secret\r\nX-Secret: s\r\n")
h = (j or {}).get("headers", {})
leaked = [k for k in ("proxy-authorization", "te", "upgrade") if k in h]
results.append(("PX-4", "hop-by-hop", st, leaked))
if leaked:
    bad.append(f"PX-4 hop-by-hop headers forwarded: {leaked}")

# PX-5 multiple Set-Cookie preserved separately; no Set-Cookie merging
st, hd, j = get("/api/health")
sc = [v.strip() for k, v in hd if k.lower() == "set-cookie"]
results.append(("PX-5", "set-cookie", st, sc))
if len(sc) != 2:
    bad.append(f"PX-5 Set-Cookie not preserved as 2 headers: {sc}")

# PX-6 unreachable backend: structured 502, no internal address or stack in the body
results.append(("PX-6", "see run.sh", None, None))

# PX-7 absolute-form / scheme-relative targets stay on the configured backend
for target in ["http://evil.example/api/health", "/api//evil.example/x", "/api/@evil.example/x"]:
    try:
        st, hd, j = raw(f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\nConnection: close\r\n\r\n".encode())
    except Exception as exc:  # noqa: BLE001
        st, j = f"EXC {type(exc).__name__}", None
    results.append(("PX-7", target, st, (j or {}).get("raw_path") if isinstance(j, dict) else None))

# PX-8 cookie forwarded only to the configured backend (the echo IS the configured backend)
st, hd, j = get("/api/health", "Cookie: access_token=SYNTHETIC; csrf_token=SYNTHETIC\r\n")
results.append(("PX-8", "cookie", st, "cookie" in (j or {}).get("headers", {})))

for r in results:
    print("PX|" + "|".join(map(str, r)))
print("PX_BAD|" + json.dumps(bad))
# pytest-style lines so first_cycle_acceptance_v1/summarize.py can gate them
for cid in ("PX-1_traversal_stays_in_api", "PX-2_client_xff_not_trusted", "PX-3_host_not_forwarded", "PX-4_hop_by_hop_stripped",
            "PX-5_set_cookie_preserved"):
    failed = any(b.startswith(cid.split("_")[0] + " ") for b in bad)
    print(f"{'FAILED' if failed else 'PASSED'} proxy::{cid}")
sys.exit(1 if bad else 0)
