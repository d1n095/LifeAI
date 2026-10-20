"""Recording stand-in for the FastAPI backend (loopback only). Echoes what the proxy actually forwarded."""
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    def _go(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        out = {"method": self.command, "raw_path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
               "body_len": len(body), "peer": self.client_address[0]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("set-cookie", "a=1; Path=/; HttpOnly; Secure")
        self.send_header("set-cookie", "b=2; Path=/; HttpOnly; Secure")
        self.send_header("x-internal-debug", "backend-header")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _go

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
