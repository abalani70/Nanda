#!/usr/bin/env python3
"""Watchtower web app: provider portal, payer agent and a spoofed agent, shown live.

The provider submits a prior auth from the browser and watches its status. The same
agents as run_demo.py do the work; every signed message, index lookup and human
checkpoint is animated on a graph. Human checkpoints become buttons in the page.

  python web_app.py                  # http://127.0.0.1:8080  (offline MockIndex)
  python web_app.py --live-index     # discover/verify through api.nandaindex.org
  python web_app.py --host 0.0.0.0 --port 9000

Stdlib only (plus optional `cryptography` for real Ed25519). Data is synthetic.
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pa_demo import console as c
from pa_demo import emr
from pa_demo.identity import CRYPTO_MODE, KeyPair
from pa_demo.index import MockIndex
from pa_demo.oversight import Oversight
from pa_demo.payer_agent import POLICY, PayerAgent
from pa_demo.provider_agent import ProviderAgent, SpoofedProvider
from pa_demo.transport import Transport

PAYER_ORG = "Example Health Plan"
HTML_FILE = Path(__file__).parent / "web" / "index.html"
ROGUE_ID = "urn:air:rogue.example:agent:prior-auth"
HINTS = {
    "pt-1001": "complete chart",
    "pt-1002": "PT notes held back (minimum necessary)",
    "pt-1003": "no conservative therapy yet",
}

MAX_BODY = 64 * 1024        # largest POST body the API accepts
MAX_RUNNING = 20            # cap on tasks in flight (each holds a thread)
CHECKPOINT_TIMEOUT = 900    # seconds a checkpoint waits for a person
INDEX_CACHE_TTL = 60        # live index: re-resolve AgentFacts after this many seconds

_ctx = threading.local()  # which case / actor the current thread is working for


class CheckpointTimeout(RuntimeError):
    """No person answered a checkpoint: stop the task rather than decide for them."""


class LocalTransport(Transport):
    """Only delivers to agents running in this process.

    The watchtower hosts both agents itself, so an endpoint taken from the index that is
    not one of ours (e.g. an http:// URL in a live-index record) is refused instead of
    being sent patient data.
    """

    def __call__(self, env, endpoint):
        if endpoint not in self.local:
            raise RuntimeError(f"refusing to send to {endpoint!r}: not an agent hosted by this "
                               f"watchtower (register the agents without --http)")
        return super().__call__(env, endpoint)


def _node(label: str) -> str:
    label = label.lower()
    return "rogue" if label.startswith("rogue") else "payer" if label.startswith("payer") else "provider"


# ------------------------------------------------------------------ event log
class EventLog:
    def __init__(self):
        self.lock = threading.Lock()
        self.events: list[dict] = []
        self.cases: dict[int, dict] = {}
        self._case_ids = itertools.count(1)
        self._seq = itertools.count(1)   # never reset, so open tabs keep a valid cursor

    def record(self, kind: str, data: dict, case_id=None) -> dict:
        with self.lock:
            ev = {"seq": next(self._seq), "ts": time.time(),
                  "case": case_id if case_id is not None else getattr(_ctx, "case", None),
                  "kind": kind, **data}
            self.events.append(ev)
            case = self.cases.get(ev["case"])
            if case is not None:
                if kind == "outcome":
                    case["status"] = data["status"]
                    case["summary"] = data.get("detail", "")
                elif kind == "checkpoint":
                    case["status"] = "AWAITING HUMAN"
                elif kind == "human":
                    case["human_touches"] += 1
                elif kind == "alert":
                    case["human_touches"] += 1
            return ev

    def new_case(self, kind: str, **meta) -> dict | None:
        with self.lock:
            if sum(1 for cs in self.cases.values() if cs["running"]) >= MAX_RUNNING:
                return None
            cid = next(self._case_ids)
            case = {"id": cid, "kind": kind, "status": "SUBMITTED", "summary": "",
                    "human_touches": 0, "running": True, "created": time.time(), **meta}
            self.cases[cid] = case
        self.record("case", {"meta": {k: v for k, v in case.items() if k != "running"}}, cid)
        return case

    def on_console(self, kind: str, data: dict) -> None:
        """Turn console output from the agents into structured events."""
        if kind == "send":
            env = data["env"]
            self.record("send", {
                "from_node": _node(data["frm"]), "to_node": _node(data["to"]),
                "from_label": data["frm"], "to_label": data["to"],
                "env": {k: env.get(k) for k in ("id", "thread_id", "from", "to", "type", "ts", "sig", "body")},
            })
        elif kind in ("ok", "bad", "step"):
            text = data["text"]
            actor = text.split(":", 1)[0] if re.match(r"^(provider|payer):", text) else None
            self.record(kind, {"text": text, "node": actor})
        else:
            self.record(kind, data)


class WatchedIndex:
    """Wraps the index so every discovery/resolution shows up on the graph."""

    def __init__(self, inner, log: EventLog):
        self.inner, self.log = inner, log

    def find(self, capability, organization=None):
        res = self.inner.find(capability, organization)
        self.log.record("index", {"op": "find", "by": getattr(_ctx, "actor", "provider"),
                                  "query": capability, "org": organization,
                                  "result": [r["agent_id"] for r in res]})
        return res

    def resolve(self, agent_id):
        res = self.inner.resolve(agent_id)
        self.log.record("index", {"op": "resolve", "by": "payer", "query": agent_id,
                                  "found": res is not None,
                                  "organization": res["organization"] if res else None})
        return res

    def __getattr__(self, name):
        return getattr(self.inner, name)


class WebOversight(Oversight):
    """Human checkpoints answered from the browser (or scripted when auto mode is on)."""

    def __init__(self, log: EventLog):
        super().__init__(scripted={"confirm_denial": True, "release_phi": True})
        self.log = log
        self.auto = False
        self.pending: dict[int, dict] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def ask(self, who, question, detail, key, default, labels=("APPROVED", "REJECTED")):
        question = re.sub(r"\s*Type y to.*$", "", question)
        c.human(who, question, detail)
        if self.auto:
            decision, how = self.scripted.get(key, default), "scripted"
        else:
            cp = {"id": next(self._ids), "case": _ctx.case, "who": who, "question": question,
                  "detail": detail, "labels": list(labels),
                  "side": "provider" if who.lower().startswith("provider") else "payer",
                  "_event": threading.Event(), "_decision": default}
            with self._lock:
                self.pending[cp["id"]] = cp
            self.log.record("checkpoint", {k: v for k, v in cp.items() if not k.startswith("_")})
            answered = cp["_event"].wait(timeout=CHECKPOINT_TIMEOUT)
            with self._lock:
                self.pending.pop(cp["id"], None)
            if not answered:
                self.log.record("decided", {"who": who, "approved": None, "how": "timed out",
                                            "word": "NO ANSWER - task stopped"})
                raise CheckpointTimeout(f"no answer from {who}; nothing was released or decided")
            decision, how = cp["_decision"], "human"
        c.human_decision(who, labels[0] if decision else labels[1])
        self.log.record("decided", {"who": who, "approved": decision, "how": how,
                                    "word": labels[0] if decision else labels[1]})
        self.touches.append({"who": who, "question": question, "approved": decision})
        return decision

    def decide(self, cid: int, approve: bool) -> bool:
        """First answer wins; later or unknown answers are rejected."""
        with self._lock:
            cp = self.pending.pop(cid, None)
            if not cp:
                return False
            cp["_decision"] = approve
            cp["_event"].set()
        return True


# ------------------------------------------------------------------ app
class App:
    def __init__(self, live_index: bool):
        self.log = EventLog()
        c.subscribe(self.log.on_console)
        self.oversight = WebOversight(self.log)
        self.transport = LocalTransport()
        endpoint = "inproc://payer"
        if live_index:
            import run_demo
            from pa_demo import nanda_index as ni
            cfg = ni.load_config()
            payer_id, provider_id = (ni.agent_identity(cfg, r) for r in ("payer", "provider"))
            inner = ni.NandaIndexClient(cfg["base_url"], cache_ttl=INDEX_CACHE_TTL)
            keys = ni.STATE_DIR / "keys"
            self.index = WatchedIndex(inner, self.log)
            self.payer = PayerAgent(self.index, self.oversight, endpoint=endpoint,
                                    agent_id=payer_id["agent_id"],
                                    keys=KeyPair.load_or_create(keys / f"{payer_id['org_id']}.key"))
            self.provider = ProviderAgent(self.index, self.oversight, self.transport,
                                          agent_id=provider_id["agent_id"],
                                          keys=KeyPair.load_or_create(keys / f"{provider_id['org_id']}.key"))
            run_demo.preflight(inner, [self.payer, self.provider])
            self.index_label = f"NANDA Index ({cfg['base_url']})"
        else:
            inner = MockIndex()
            self.index = WatchedIndex(inner, self.log)
            self.payer = PayerAgent(self.index, self.oversight, endpoint=endpoint)
            self.provider = ProviderAgent(self.index, self.oversight, self.transport)
            inner.register(self.payer.facts)
            inner.register(self.provider.facts)
            self.index_label = "MockIndex (offline)"
        self.transport.register_local(endpoint, self.payer)

    # -- actions
    def _run(self, case, actor, fn):
        def work():
            _ctx.case, _ctx.actor = case["id"], actor
            try:
                res = fn()
            except Exception as e:  # surface failures in the UI instead of a dead thread
                res = {"status": "ERROR", "summary": str(e)}
            c.outcome(res["status"], res.get("summary", ""))
            self.log.record("done", {"status": res["status"], "result": res})
            with self.log.lock:
                case["running"] = False
                case["result"] = res
        threading.Thread(target=work, daemon=True).start()

    def submit(self, patient_id: str) -> dict:
        p = emr.get_patient(patient_id)
        case = self.log.new_case("provider", patient_id=patient_id, patient=p["name"],
                                 member_id=p["member_id"], service=p["service_request"]["display"],
                                 cpt=p["service_request"]["code"])
        if case is None:
            return None
        self._run(case, "provider", lambda: self.provider.request_prior_auth(patient_id, PAYER_ORG))
        return self._snap(case)

    def spoof(self, mode: str) -> dict:
        claimed = self.provider.AGENT_ID if mode == "impersonate" else ROGUE_ID
        label = ("Impersonates Riverside (wrong signing key)" if mode == "impersonate"
                 else "Unregistered agent (not in index)")
        case = self.log.new_case("rogue", attack=mode, patient="pt-9999 (forged)",
                                 service="MRI lumbar spine (forged note)", label=label,
                                 claimed_id=claimed)
        if case is None:
            return None

        def attack():
            payer = self.index.find("prior-auth.review", PAYER_ORG)[0]
            return SpoofedProvider(self.transport, claimed_id=claimed).attack(payer)
        self._run(case, "rogue", attack)
        return self._snap(case)

    def _snap(self, case: dict) -> dict:
        with self.log.lock:
            return dict(case)

    def reset(self) -> bool:
        # Same lock as new_case, so a task can't start between the check and the clear.
        with self.log.lock:
            if self.oversight.pending or any(cs["running"] for cs in self.log.cases.values()):
                return False
            self.log.events.clear()
            self.log.cases.clear()
            self.transport.trace.clear()
            self.oversight.touches.clear()
            self.payer.cases.clear()   # drop the insurer's copies of the clinical documents
        return True

    # -- views
    def bootstrap(self) -> dict:
        patients = []
        for pid, p in emr.PATIENTS.items():
            withhold = set(p.get("initially_withhold", []))
            patients.append({
                "id": pid, "name": p["name"], "dob": p["dob"], "member_id": p["member_id"],
                "hint": HINTS.get(pid, ""), "conditions": p["conditions"],
                "service_request": p["service_request"], "red_flags": p["red_flags"],
                "documents": [{**d, "withheld": d["type"] in withhold} for d in p["documents"]],
            })
        return {"patients": patients, "agents": self.index.all(), "crypto": CRYPTO_MODE,
                "index_label": self.index_label, "policy": POLICY, "payer_org": PAYER_ORG,
                "rogue_id": ROGUE_ID}

    def state(self, since: int) -> dict:
        with self.log.lock:
            events = [e for e in self.log.events if e["seq"] > since]
            cases = [dict(cs) for cs in self.log.cases.values()]
        return {"events": events, "cases": cases, "auto": self.oversight.auto,
                "pending": [k for k in self.oversight.pending]}

    def trace(self) -> dict:
        with self.log.lock:
            return {"index": self.index.all(), "messages": list(self.transport.trace),
                    "human_touches": list(self.oversight.touches),
                    "cases": [dict(cs) for cs in self.log.cases.values()]}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload, ctype="application/json"):
            out = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(out)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._send(200, HTML_FILE.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/bootstrap":
                return self._send(200, app.bootstrap())
            if url.path == "/api/state":
                since = int(parse_qs(url.query).get("since", ["0"])[0] or 0)
                return self._send(200, app.state(since))
            if url.path == "/api/trace":
                return self._send(200, app.trace())
            self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, {"error": "bad Content-Length"})
            if n < 0 or n > MAX_BODY:
                return self._send(413, {"error": "request body too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            path = urlparse(self.path).path
            if path == "/api/submit":
                if body.get("patient_id") not in emr.PATIENTS:
                    return self._send(400, {"error": "unknown patient"})
                case = app.submit(body["patient_id"])
                return self._send(200, case) if case else self._send(429, {"error": "too many tasks in flight"})
            if path == "/api/spoof":
                case = app.spoof(body.get("mode", "impersonate"))
                return self._send(200, case) if case else self._send(429, {"error": "too many tasks in flight"})
            if path == "/api/decide":
                if not isinstance(body.get("approve"), bool) or not isinstance(body.get("id"), int):
                    return self._send(400, {"error": "need integer id and boolean approve"})
                ok = app.oversight.decide(body["id"], body["approve"])
                return self._send(200 if ok else 409, {"ok": ok})
            if path == "/api/mode":
                if not isinstance(body.get("auto"), bool):
                    return self._send(400, {"error": "need boolean auto"})
                app.oversight.auto = body["auto"]
                return self._send(200, {"auto": app.oversight.auto})
            if path == "/api/reset":
                ok = app.reset()
                return self._send(200 if ok else 409,
                                  {"ok": ok, "error": None if ok else "finish open cases first"})
            self._send(404, {"error": "not found"})

        def log_message(self, *a):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--live-index", action="store_true",
                    help="use the live NANDA Index (agents must be registered; see README)")
    ap.add_argument("--open", action="store_true", help="open the browser")
    ap.add_argument("--allow-remote", action="store_true",
                    help="allow a non-loopback --host (the API has no login: trusted networks only)")
    args = ap.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.allow_remote:
        sys.exit("error: the watchtower API has no authentication, so it only binds to loopback.\n"
                 "  Add --allow-remote to serve it on a trusted network anyway.")
    try:
        app = App(args.live_index)
    except (RuntimeError, OSError) as e:
        sys.exit(f"error: {e}")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}/"
    c.banner(f"Prior-auth watchtower running at {url}")
    c.step(f"signatures: {CRYPTO_MODE} · index: {app.index_label}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
