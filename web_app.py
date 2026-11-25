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

_ctx = threading.local()  # which case / actor the current thread is working for


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

    def record(self, kind: str, data: dict, case_id=None) -> dict:
        with self.lock:
            ev = {"seq": len(self.events) + 1, "ts": time.time(),
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

    def new_case(self, kind: str, **meta) -> dict:
        with self.lock:
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
            self.pending[cp["id"]] = cp
            self.log.record("checkpoint", {k: v for k, v in cp.items() if not k.startswith("_")})
            answered = cp["_event"].wait(timeout=900)
            self.pending.pop(cp["id"], None)
            decision, how = cp["_decision"], ("human" if answered else "timeout default")
        c.human_decision(who, labels[0] if decision else labels[1])
        self.log.record("decided", {"who": who, "approved": decision, "how": how,
                                    "word": labels[0] if decision else labels[1]})
        self.touches.append({"who": who, "question": question, "approved": decision})
        return decision

    def decide(self, cid: int, approve: bool) -> bool:
        cp = self.pending.get(cid)
        if not cp:
            return False
        cp["_decision"] = bool(approve)
        cp["_event"].set()
        return True


# ------------------------------------------------------------------ app
class App:
    def __init__(self, live_index: bool):
        self.log = EventLog()
        c.subscribe(self.log.on_console)
        self.oversight = WebOversight(self.log)
        self.transport = Transport()
        endpoint = "inproc://payer"
        if live_index:
            import run_demo
            from pa_demo import nanda_index as ni
            cfg = ni.load_config()
            payer_id, provider_id = (ni.agent_identity(cfg, r) for r in ("payer", "provider"))
            inner = ni.NandaIndexClient(cfg["base_url"])
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
            case["running"] = False
            case["result"] = res
        threading.Thread(target=work, daemon=True).start()

    def submit(self, patient_id: str) -> dict:
        p = emr.get_patient(patient_id)
        case = self.log.new_case("provider", patient_id=patient_id, patient=p["name"],
                                 member_id=p["member_id"], service=p["service_request"]["display"],
                                 cpt=p["service_request"]["code"])
        self._run(case, "provider", lambda: self.provider.request_prior_auth(patient_id, PAYER_ORG))
        return case

    def spoof(self, mode: str) -> dict:
        claimed = self.provider.AGENT_ID if mode == "impersonate" else ROGUE_ID
        label = ("Impersonates Riverside (wrong signing key)" if mode == "impersonate"
                 else "Unregistered agent (not in index)")
        case = self.log.new_case("rogue", attack=mode, patient="pt-9999 (forged)",
                                 service="MRI lumbar spine (forged note)", label=label,
                                 claimed_id=claimed)

        def attack():
            payer = self.index.find("prior-auth.review", PAYER_ORG)[0]
            return SpoofedProvider(self.transport, claimed_id=claimed).attack(payer)
        self._run(case, "rogue", attack)
        return case

    def reset(self) -> bool:
        if self.oversight.pending or any(cs["running"] for cs in self.log.cases.values()):
            return False
        with self.log.lock:
            self.log.events.clear()
            self.log.cases.clear()
        self.transport.trace.clear()
        self.oversight.touches.clear()
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
            cases = list(self.log.cases.values())
        return {"events": events, "cases": cases, "auto": self.oversight.auto,
                "pending": [k for k in self.oversight.pending]}

    def trace(self) -> dict:
        return {"index": self.index.all(), "messages": self.transport.trace,
                "human_touches": self.oversight.touches,
                "cases": list(self.log.cases.values())}


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
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            path = urlparse(self.path).path
            if path == "/api/submit":
                if body.get("patient_id") not in emr.PATIENTS:
                    return self._send(400, {"error": "unknown patient"})
                return self._send(200, app.submit(body["patient_id"]))
            if path == "/api/spoof":
                return self._send(200, app.spoof(body.get("mode", "impersonate")))
            if path == "/api/decide":
                ok = app.oversight.decide(int(body.get("id", 0)), bool(body.get("approve")))
                return self._send(200 if ok else 409, {"ok": ok})
            if path == "/api/mode":
                app.oversight.auto = bool(body.get("auto"))
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
    args = ap.parse_args()
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
