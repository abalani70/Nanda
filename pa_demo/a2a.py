"""Agent2Agent (A2A) protocol v1.0, JSON-RPC binding. Stdlib only.

The insurer agent is an A2A *server*: it publishes an Agent Card at
``/.well-known/agent-card.json`` and answers JSON-RPC ``SendMessage`` / ``GetTask``
at its endpoint. The provider agent (and the spoofed agent) are A2A *clients*.

Wire format follows the v1.0 spec (a2aproject/A2A, specification/a2a.proto):
camelCase fields, PascalCase methods, ``TASK_STATE_*`` / ``ROLE_*`` enum strings,
``A2A-Version: 1.0`` header. Each A2A message carries the demo's signed envelope
(identity.seal) as a structured-data part, so the receiver still verifies the
sender against its key in the NANDA Index. A2A provides the conversation:
tasks, ``TASK_STATE_INPUT_REQUIRED`` for "send more info", and multi-turn
continuation by ``taskId``.

Endpoints are ``http(s)://…`` (real HTTP) or ``inproc://…`` (same A2A JSON-RPC
frames, delivered by function call; used by the CLI's default offline run).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import console as c

A2A_VERSION = "1.0"
CARD_PATH = "/.well-known/agent-card.json"
ENVELOPE_MEDIA_TYPE = "application/vnd.halhack.signed-envelope+json"
MAX_BODY = 256 * 1024

# JSON-RPC / A2A error codes (spec §5.4)
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = (
    -32700, -32600, -32601, -32602, -32603)
TASK_NOT_FOUND, UNSUPPORTED_OPERATION, CONTENT_TYPE_NOT_SUPPORTED, VERSION_NOT_SUPPORTED = (
    -32001, -32004, -32005, -32009)

# Demo decision -> A2A task state
STATE_FOR = {
    "APPROVED": "TASK_STATE_COMPLETED",
    "DENIED": "TASK_STATE_COMPLETED",
    "PENDED": "TASK_STATE_INPUT_REQUIRED",
    "REJECTED": "TASK_STATE_REJECTED",
}
TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}


class A2AError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(f"A2A error {code}: {message}")
        self.code, self.message = code, message


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


def _id() -> str:
    return str(uuid.uuid4())


def card_url(endpoint: str) -> str:
    """Well-known Agent Card URL for an endpoint (inproc endpoints keep their scheme)."""
    p = urlsplit(endpoint)
    return f"{p.scheme}://{p.netloc}{CARD_PATH}"


def envelope_part(env: dict) -> dict:
    return {"data": env, "mediaType": ENVELOPE_MEDIA_TYPE}


def find_envelope(parts) -> dict | None:
    for part in parts or []:
        if isinstance(part, dict) and part.get("mediaType") == ENVELOPE_MEDIA_TYPE \
                and isinstance(part.get("data"), dict):
            return part["data"]
    return None


# ======================================================================= server
class A2AServer:
    """Wraps an agent exposing ``handle(envelope) -> envelope`` as an A2A JSON-RPC server.

    Tasks are bound to the signed sender that opened them and to the envelope thread they
    carry; only that sender can continue a task, one continuation at a time. Finished tasks
    are kept (bounded, oldest first) without their clinical content.
    """

    MAX_TASKS = 500          # retained tasks; oldest finished ones are dropped first
    MAX_SEEN = 2000          # remembered messageIds for duplicate detection

    def __init__(self, agent, skills: list[dict], description: str, on_request=None):
        self.agent = agent
        self.on_request = on_request          # optional hook(env) before the agent runs
        self.tasks: dict[str, dict] = {}      # taskId -> task (insertion ordered)
        self._meta: dict[str, dict] = {}      # taskId -> {"owner", "thread"}
        self._threads: dict[str, str] = {}    # envelope thread_id -> taskId
        self._seen: dict[tuple, str] = {}     # (sender, messageId) -> taskId
        self.active = 0                       # requests being processed right now
        self._lock = threading.Lock()
        f = agent.facts
        self.card = {
            "name": f["name"],
            "description": description,
            "supportedInterfaces": [
                {"url": f["endpoint"], "protocolBinding": "JSONRPC", "protocolVersion": A2A_VERSION}],
            "provider": {"organization": f["organization"], "url": "https://example.com/"},
            "version": "1.0.0",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": [ENVELOPE_MEDIA_TYPE],
            "defaultOutputModes": [ENVELOPE_MEDIA_TYPE, "text/plain"],
            "skills": skills,
        }

    def clear(self) -> None:
        with self._lock:
            self.tasks.clear(); self._meta.clear(); self._threads.clear(); self._seen.clear()

    # ------------------------------------------------------------- JSON-RPC
    def handle_rpc(self, req, version: str | None) -> dict:
        rid = req.get("id") if isinstance(req, dict) else None
        if not isinstance(rid, (str, int, type(None))) or isinstance(rid, bool):
            rid = None
        try:
            if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
                raise A2AError(INVALID_REQUEST, "not a JSON-RPC 2.0 request")
            if (version or "0.3") != A2A_VERSION:
                raise A2AError(VERSION_NOT_SUPPORTED, f"A2A-Version {version or '(empty = 0.3)'} not supported; use {A2A_VERSION}")
            params = req.get("params") or {}
            if not isinstance(params, dict):
                raise A2AError(INVALID_PARAMS, "params must be an object")
            if req["method"] == "SendMessage":
                result = {"task": self._send_message(params)}
            elif req["method"] == "GetTask":
                result = self._get_task(params)
            else:
                raise A2AError(METHOD_NOT_FOUND, f"method {req['method']!r} not supported")
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except A2AError as e:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": e.message}}
        except Exception:  # never drop a request without a JSON-RPC answer
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": INTERNAL_ERROR, "message": "internal error"}}

    @staticmethod
    def _str_id(value, name: str):
        if value is not None and not isinstance(value, str):
            raise A2AError(INVALID_PARAMS, f"{name} must be a string")
        return value

    def _get_task(self, params) -> dict:
        """GetTask carries no signed identity, so it only reveals the task's state.

        History, artifacts and the decision (all of which hold clinical data) go only to
        the signed sender, in SendMessage replies.
        """
        task_id = self._str_id(params.get("id"), "id")
        with self._lock:
            task = self.tasks.get(task_id) if task_id else None
            if task is None:
                raise A2AError(TASK_NOT_FOUND, "task not found")
            return {"id": task["id"], "contextId": task["contextId"],
                    "status": {"state": task["status"]["state"], "timestamp": task["status"].get("timestamp")}}

    def _send_message(self, params) -> dict:
        msg = params.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "ROLE_USER" or not isinstance(msg.get("parts"), list):
            raise A2AError(INVALID_PARAMS, "message needs messageId, role ROLE_USER and parts")
        message_id = self._str_id(msg.get("messageId"), "messageId")
        task_id = self._str_id(msg.get("taskId"), "taskId")
        context_id = self._str_id(msg.get("contextId"), "contextId")
        if not message_id:
            raise A2AError(INVALID_PARAMS, "message needs a messageId")
        env = find_envelope(msg["parts"])
        if env is None:
            raise A2AError(CONTENT_TYPE_NOT_SUPPORTED, f"expected a {ENVELOPE_MEDIA_TYPE} data part")
        sender, thread = env.get("from"), env.get("thread_id")
        if not isinstance(sender, str) or not isinstance(thread, str):
            raise A2AError(INVALID_PARAMS, "envelope needs from and thread_id")

        with self._lock:
            # Duplicate delivery of a message we already processed: return the same task.
            done = self._seen.get((sender, message_id))
            if done and done in self.tasks:
                return self._public(self.tasks[done])
            existing = None
            if task_id:
                existing = self.tasks.get(task_id)
                meta = self._meta.get(task_id, {})
                # Not found == not yours (spec §3.3.2): don't reveal other senders' tasks.
                if existing is None or meta.get("owner") != sender:
                    raise A2AError(TASK_NOT_FOUND, "task not found")
                if meta.get("thread") != thread:
                    raise A2AError(INVALID_PARAMS, "envelope thread does not belong to this task")
                if context_id and context_id != existing["contextId"]:
                    raise A2AError(INVALID_PARAMS, "contextId does not match the task")
                if existing["status"]["state"] != "TASK_STATE_INPUT_REQUIRED":
                    raise A2AError(UNSUPPORTED_OPERATION, f"task is {existing['status']['state']}")
                existing["status"] = {"state": "TASK_STATE_WORKING", "timestamp": _now()}  # claim it
            elif thread in self._threads:
                # This conversation already has a task: follow-ups must name it.
                raise A2AError(INVALID_PARAMS, "this thread already has a task; continue it with taskId")
            self.active += 1

        try:
            if self.on_request:
                self.on_request(env)
            failure = None
            try:
                reply = self.agent.handle(env)
            except Exception as e:  # the agent failed (e.g. no human answered): fail the task
                reply, failure = None, str(e)
            return self._record(msg, env, existing, context_id, reply, failure)
        finally:
            with self._lock:
                self.active -= 1

    def _record(self, msg, env, existing, context_id, reply, failure) -> dict:
        sender, thread = env["from"], env["thread_id"]
        rejected = reply is not None and reply["body"].get("status") == "REJECTED"
        with self._lock:
            if existing is not None and rejected:
                # A rejected message never changes an existing task; put the task back.
                existing["status"] = {"state": "TASK_STATE_INPUT_REQUIRED", "timestamp": _now()}
                existing = None
            task = existing or {"id": _id(), "contextId": context_id or _id(),
                                "status": {"state": "TASK_STATE_SUBMITTED"}, "artifacts": [], "history": []}
            if reply is None:
                state, parts = "TASK_STATE_FAILED", [{"text": f"task failed: {failure}"}]
            else:
                state = STATE_FOR.get(reply["body"].get("status"), "TASK_STATE_FAILED")
                parts = [envelope_part(reply), {"text": reply["body"].get("summary", "")}]
            status_msg = {"messageId": _id(), "contextId": task["contextId"], "taskId": task["id"],
                          "role": "ROLE_AGENT", "parts": parts}
            task["status"] = {"state": state, "message": status_msg, "timestamp": _now()}
            task["history"].append({**msg, "taskId": task["id"], "contextId": task["contextId"]})
            task["history"].append(status_msg)
            if state == "TASK_STATE_COMPLETED":
                task["artifacts"] = [{"artifactId": _id(), "name": "prior-auth-decision",
                                      "parts": [{"data": reply["body"], "mediaType": "application/json"}]}]
            self.tasks[task["id"]] = task
            if not rejected:
                self._meta.setdefault(task["id"], {"owner": sender, "thread": thread})
                self._threads[thread] = task["id"]
            self._seen[(sender, msg["messageId"])] = task["id"]
            result = self._public(task)
            if state in TERMINAL:
                # The reply above went to the signed sender; keep only the outcome from here on.
                task["history"], task["artifacts"] = [], []
                task["status"] = {"state": state, "timestamp": task["status"]["timestamp"]}
                if not rejected and hasattr(self.agent, "forget"):
                    self.agent.forget(thread)
            self._trim()
            return result

    @staticmethod
    def _public(task: dict) -> dict:
        return json.loads(json.dumps(task))   # snapshot

    def _trim(self) -> None:
        while len(self.tasks) > self.MAX_TASKS:
            victim = next((tid for tid, t in self.tasks.items() if t["status"]["state"] in TERMINAL),
                          next(iter(self.tasks)))
            self.tasks.pop(victim)
            meta = self._meta.pop(victim, None)
            if meta and self._threads.get(meta["thread"]) == victim:
                self._threads.pop(meta["thread"])
        while len(self._seen) > self.MAX_SEEN:
            self._seen.pop(next(iter(self._seen)))


def serve(server: A2AServer, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Serve an A2A server over HTTP in a background thread (Agent Card + JSON-RPC)."""
    rpc_path = urlsplit(server.card["supportedInterfaces"][0]["url"]).path or "/"

    class Handler(BaseHTTPRequestHandler):
        timeout = 15   # seconds; a stalled client can't hold a thread forever

        def _json(self, code, obj, extra=None):
            out = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):  # noqa: N802
            if self.path == CARD_PATH:
                return self._json(200, server.card, {"Cache-Control": "max-age=60"})
            self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != rpc_path:
                return self._json(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return self._json(413, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": INVALID_REQUEST, "message": "request too large"}})
            try:
                req = json.loads(self.rfile.read(n) or b"null")
            except ValueError:
                return self._json(200, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": PARSE_ERROR, "message": "invalid JSON"}})
            self._json(200, server.handle_rpc(req, self.headers.get("A2A-Version")))

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# ======================================================================= client
class A2AClient:
    """Sends signed envelopes to A2A servers. Callable like the old transport:
    ``client(envelope, endpoint) -> reply envelope``.

    ``allowed`` (optional) limits which endpoints may receive anything at all.
    """

    def __init__(self, allowed: set[str] | None = None, timeout: float = 1000):
        self.local: dict[str, A2AServer] = {}
        self.allowed = allowed
        self.timeout = timeout          # blocking SendMessage waits through human review
        self.trace: list[dict] = []     # signed envelopes, both directions
        self.frames: list[dict] = []    # raw A2A JSON-RPC request/response pairs
        self._tasks: dict[str, str] = {}   # envelope thread_id -> A2A taskId
        self._rpc_ids = iter(range(1, 1 << 62))

    def register_local(self, endpoint: str, server: A2AServer) -> None:
        self.local[endpoint] = server

    def _check(self, endpoint: str) -> None:
        if self.allowed is not None and endpoint not in self.allowed:
            raise RuntimeError(f"refusing to contact {endpoint!r}: not an allowed A2A endpoint")
        if not (endpoint.startswith(("http://", "https://")) or endpoint in self.local):
            raise RuntimeError(f"no A2A server at {endpoint!r}")

    # ------------------------------------------------------------- discovery
    def agent_card(self, endpoint: str) -> dict:
        """Fetch and check the Agent Card; return it."""
        self._check(endpoint)
        url = card_url(endpoint)
        if endpoint in self.local:
            card = json.loads(json.dumps(self.local[endpoint].card))
        else:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "A2A-Version": A2A_VERSION})
            with urllib.request.urlopen(req, timeout=10) as r:
                card = json.loads(r.read())
        iface = self._interface(card, endpoint)
        c.agent_card(url, card, iface)
        return card

    @staticmethod
    def _interface(card: dict, endpoint: str) -> dict:
        for iface in card.get("supportedInterfaces") or []:
            if iface.get("protocolBinding") == "JSONRPC" and iface.get("protocolVersion") == A2A_VERSION:
                if iface.get("url") != endpoint:
                    raise RuntimeError(f"Agent Card's JSON-RPC url {iface.get('url')!r} does not match "
                                       f"the endpoint published in the index ({endpoint!r})")
                return iface
        raise RuntimeError(f"agent at {endpoint} has no A2A {A2A_VERSION} JSON-RPC interface")

    # ------------------------------------------------------------- messaging
    def _rpc(self, endpoint: str, method: str, params: dict) -> dict:
        req = {"jsonrpc": "2.0", "id": next(self._rpc_ids), "method": method, "params": params}
        if endpoint in self.local:
            resp = self.local[endpoint].handle_rpc(json.loads(json.dumps(req)), A2A_VERSION)
        else:
            http = urllib.request.Request(
                endpoint, data=json.dumps(req).encode(), method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json",
                         "A2A-Version": A2A_VERSION})
            try:
                with urllib.request.urlopen(http, timeout=self.timeout) as r:
                    resp = json.loads(r.read())
            except urllib.error.HTTPError as e:
                resp = json.loads(e.read() or b"{}") or {"error": {"code": e.code, "message": e.reason}}
        self.frames.append({"endpoint": endpoint, "request": req, "response": resp})
        return resp

    def __call__(self, env: dict, endpoint: str) -> dict:
        self._check(endpoint)
        self.trace.append(env)
        msg = {"messageId": _id(), "contextId": env["thread_id"], "role": "ROLE_USER",
               "parts": [envelope_part(env), {"text": env["body"].get("summary", env["type"])}]}
        if env["thread_id"] in self._tasks:
            msg["taskId"] = self._tasks[env["thread_id"]]
        params = {"message": msg,
                  "configuration": {"acceptedOutputModes": [ENVELOPE_MEDIA_TYPE, "text/plain"]}}
        resp = self._rpc(endpoint, "SendMessage", params)
        if "error" in resp:
            err = resp["error"]
            c.a2a("SendMessage", endpoint, request=params, response=resp, error=err)
            raise A2AError(err.get("code", INTERNAL_ERROR), err.get("message", "error"))
        task = (resp.get("result") or {}).get("task") or {}
        state = task.get("status", {}).get("state")
        c.a2a("SendMessage", endpoint, task_id=task.get("id"), context_id=task.get("contextId"),
              state=state, request=params, response=resp)
        if state == "TASK_STATE_INPUT_REQUIRED":
            self._tasks[env["thread_id"]] = task["id"]
        else:
            self._tasks.pop(env["thread_id"], None)
        reply = find_envelope(task.get("status", {}).get("message", {}).get("parts"))
        if reply is None:
            texts = [p.get("text") for p in task.get("status", {}).get("message", {}).get("parts", []) if p.get("text")]
            raise RuntimeError(f"A2A task {state}: {'; '.join(texts) or 'no reply envelope'}")
        self.trace.append(reply)
        return reply
