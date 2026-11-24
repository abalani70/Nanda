"""Message transport: in-process, or real HTTP (JSON over POST) between agents."""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Transport:
    """Routes envelopes to agents and records a trace of every message."""

    def __init__(self):
        self.local: dict[str, object] = {}
        self.trace: list[dict] = []

    def register_local(self, endpoint: str, agent) -> None:
        self.local[endpoint] = agent

    def __call__(self, env: dict, endpoint: str) -> dict:
        self.trace.append(env)
        if endpoint.startswith("inproc://"):
            reply = self.local[endpoint].handle(env)
        else:
            req = urllib.request.Request(
                endpoint, data=json.dumps(env).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                reply = json.loads(r.read())
        self.trace.append(reply)
        return reply


def serve(agent, host="127.0.0.1", port=8765) -> ThreadingHTTPServer:
    """Expose an agent's handle() over HTTP POST /a2a in a background thread."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            if self.path != "/a2a":
                self.send_error(404)
                return
            env = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            out = json.dumps(agent.handle(env)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
