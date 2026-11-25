#!/usr/bin/env python3
"""Cross-organization prior authorization — agent-to-agent with human oversight.

  python run_demo.py                 # all scenarios, scripted human decisions
  python run_demo.py --interactive   # you play the humans (y/n prompts)
  python run_demo.py --http          # payer agent is a real A2A server over HTTP
  python run_demo.py --case B        # one scenario: A, B, C or S (spoof)
  python run_demo.py --mock-index    # offline: in-memory index instead of api.nandaindex.org
  python run_demo.py --register      # publish both agents' AgentFacts to the NANDA Index
  DEMO_PAUSE=0.8 python run_demo.py  # slow it down for a live audience
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections import Counter

from pa_demo import console as c
from pa_demo import nanda_index as ni
from pa_demo.identity import CRYPTO_MODE, KeyPair
from pa_demo.index import MockIndex
from pa_demo.oversight import Oversight
from pa_demo.payer_agent import A2A_DESCRIPTION, A2A_SKILLS, PayerAgent
from pa_demo.provider_agent import ProviderAgent, SpoofedProvider
from pa_demo import a2a

PAYER_ORG = "Example Health Plan"

SCENARIOS = {
    "A": ("A · Complete documentation → APPROVED", "pt-1001"),
    "B": ("B · Missing PT notes → PENDED → resubmit → APPROVED", "pt-1002"),
    "C": ("C · Insufficient therapy → proposed DENIAL → clinician decides", "pt-1003"),
    "S": ("S · Spoofed provider agent → REJECTED at identity check", None),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interactive", action="store_true", help="prompt for human decisions")
    ap.add_argument("--http", action="store_true", help="serve the payer agent as an A2A server over HTTP")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--case", choices=list(SCENARIOS), action="append",
                    help="run only these scenarios (repeatable)")
    ap.add_argument("--clinician-overrides", action="store_true",
                    help="scripted clinician overturns the proposed denial in case C")
    ap.add_argument("--mock-index", action="store_true",
                    help="use the in-memory MockIndex instead of the NANDA Index API (offline)")
    ap.add_argument("--register", action="store_true",
                    help="register/update both agents in the NANDA Index, then exit")
    ap.add_argument("--domain", help="base domain you control (agents use payer.<d>, provider.<d>)")
    ap.add_argument("--payer-email", help="mailbox that identifies the payer agent (personal entry)")
    ap.add_argument("--provider-email", help="mailbox that identifies the provider agent (personal entry)")
    args = ap.parse_args()

    oversight = Oversight(interactive=args.interactive,
                          scripted={"confirm_denial": not args.clinician_overrides,
                                    "release_phi": True})
    endpoint = f"http://127.0.0.1:{args.port}/a2a" if args.http else "inproc://payer"
    # Only our own payer endpoint may receive requests, whatever an index entry says.
    transport = a2a.A2AClient(allowed={endpoint})

    if args.mock_index and not args.register:
        index = MockIndex()
        payer = PayerAgent(index, oversight, endpoint=endpoint)
        provider = ProviderAgent(index, oversight, transport)
        index.register(payer.facts)
        index.register(provider.facts)
        index_label = "MockIndex (in-memory, offline)"
    else:
        cfg = ni.load_config()
        if args.domain:
            cfg["domain"] = args.domain.strip().lower()
        for role in ("payer", "provider"):
            if getattr(args, f"{role}_email"):
                cfg["emails"][role] = getattr(args, f"{role}_email").strip().lower()
        try:
            payer_id, provider_id = (ni.agent_identity(cfg, r) for r in ("payer", "provider"))
        except RuntimeError as e:
            sys.exit(f"error: {e}")
        index = ni.NandaIndexClient(cfg["base_url"])
        keys = ni.STATE_DIR / "keys"
        payer = PayerAgent(index, oversight, endpoint=endpoint, agent_id=payer_id["agent_id"],
                           keys=KeyPair.load_or_create(keys / f"{payer_id['org_id']}.key"))
        provider = ProviderAgent(index, oversight, transport, agent_id=provider_id["agent_id"],
                                 keys=KeyPair.load_or_create(keys / f"{provider_id['org_id']}.key"))
        if args.register:
            register(index, cfg, [(payer, payer_id), (provider, provider_id)])
            return
        preflight(index, [payer, provider])
        index_label = f"NANDA Index {cfg['base_url']}"

    a2a_server = a2a.A2AServer(payer, A2A_SKILLS, A2A_DESCRIPTION)
    if args.http:
        a2a.serve(a2a_server, port=args.port)
    else:
        transport.register_local(endpoint, a2a_server)

    c.banner("Prior authorization across two organizations — NANDA-style agents + HAL oversight")
    c.step(f"signatures: {CRYPTO_MODE}"
           + ("  (install `cryptography` for real Ed25519)" if CRYPTO_MODE != "ed25519" else ""))
    c.step(f"transport: A2A {a2a.A2A_VERSION} JSON-RPC "
           + (f"over HTTP {endpoint}" if args.http else "(in-process; --http for a real server)"))
    c.step(f"index: {index_label}")
    for f in index.all():
        c.step(f"index: {f['agent_id']}  [{f['organization']}]  caps={f['capabilities']}")

    results = []
    for key in args.case or list(SCENARIOS):
        title, patient = SCENARIOS[key]
        c.banner(title)
        touches_before = len(oversight.touches)
        msgs_before = len(transport.trace)
        if patient:
            res = provider.request_prior_auth(patient, PAYER_ORG)
        else:
            res = SpoofedProvider(transport, claimed_id=provider.AGENT_ID).attack(
                index.find("prior-auth.review", PAYER_ORG)[0])
        c.outcome(res["status"], res.get("summary", ""))
        results.append({
            "scenario": key, "status": res["status"],
            "messages": len(transport.trace) - msgs_before,
            "human_touches": len(oversight.touches) - touches_before,
        })

    # ---------------------------------------------------------------- summary
    c.banner("What changed: humans only saw the decisions that needed judgment")
    print(f"  {'Scenario':<10}{'Outcome':<12}{'Messages':<10}{'Human touches'}")
    for r in results:
        print(f"  {r['scenario']:<10}{r['status']:<12}{r['messages']:<10}{r['human_touches']}")
    counts = Counter(r["status"] for r in results)
    total_h = sum(r["human_touches"] for r in results)
    auto = sum(1 for r in results if r["human_touches"] == 0)
    print(f"\n  {len(results)} cases · {auto} fully agent-to-agent · {total_h} human touch(es) · "
          + ", ".join(f"{k.lower()}={v}" for k, v in counts.items()))

    with open("trace.json", "w") as fh:
        json.dump({"index": index.all(), "messages": transport.trace, "a2a": transport.frames,
                   "human_touches": oversight.touches, "results": results}, fh, indent=2)
    c.step("full signed message trace written to trace.json")


# ------------------------------------------------------------- NANDA Index setup
def preflight(index, agents):
    """Before a live run: both agents must be in the index with the keys we hold."""
    try:
        index.health()
    except OSError as e:
        sys.exit(f"error: NANDA Index unreachable ({e}). Re-run with --mock-index to demo offline.")
    for agent in agents:
        problem = ni.facts_mismatch(agent.facts, index.resolve(agent.AGENT_ID))
        if problem:
            sys.exit(f"error: {agent.AGENT_ID}: {problem}.\n"
                     f"  Run `python run_demo.py --register` (same --http setting as the demo), "
                     f"finish domain verification, or use --mock-index.")


def register(index, cfg, agents):
    c.banner(f"Registering demo agents in the NANDA Index ({cfg['base_url']})")
    if os.environ.get("NANDA_TOKEN"):
        index.token = os.environ["NANDA_TOKEN"]
    else:
        email = os.environ.get("NANDA_EMAIL") or input("  nandaindex.org account email: ").strip()
        password = os.environ.get("NANDA_PASSWORD") or getpass.getpass("  password: ")
        try:
            index.login(email, password)
        except ni.IndexAPIError as e:
            sys.exit(f"error: login failed ({e}). Create and verify an account at "
                     f"https://nandaindex.org first.")
    needs_contact = any(ident["kind"] == "domain" for _, ident in agents)
    contact = (os.environ.get("NANDA_CONTACT_EMAIL") or os.environ.get("NANDA_EMAIL")
               or (input("  contact email for the index entries: ").strip() if needs_contact else ""))
    ni.save_config(cfg)

    pending, unverified = [], []
    for agent, ident in agents:
        try:
            rec = index.register_agent(agent.facts, ident, contact)
        except (ni.IndexAPIError, RuntimeError) as e:
            sys.exit(f"error registering {ident['org_id']}: {e}")
        c.ok(f"{ident['org_id']}: {rec['identifier']}  status={rec['status']}")
        if rec["status"] == "active":
            continue
        if ident["kind"] == "personal":
            unverified.append(ident["email"])
            continue
        try:
            rec = index.verify_domain(ident["org_id"])
            c.ok(f"{ident['org_id']}: domain verified, status={rec.get('status')}")
            if rec.get("status") == "active":
                continue
        except ni.IndexAPIError:
            pass
        ch = index.domain_challenge(ident["org_id"])
        pending.append((ident["org_id"], ch))

    if unverified:
        c.banner("Action needed: confirm the agents' email addresses")
        for email in unverified:
            print(f"  open the 'Verify your NANDA Index registration' email sent to {email}")
        print("\n  Each entry goes live once its link is confirmed (POST /api/v1/verify-email).")
    if pending:
        c.banner("Action needed: prove you control the domains (DNS TXT records)")
        for org_id, ch in pending:
            print(f"  {org_id}:  {ch['record_name']}  TXT  \"{ch['record_value']}\"")
        print("\n  Add these records at your DNS provider, wait for them to propagate, then run\n"
              "  `python run_demo.py --register` again. Entries stay hidden until verified.\n"
              "  Also click the verification link the index emails to the contact address.")
    if not pending and not unverified:
        c.ok("both agents are live in the NANDA Index — run `python run_demo.py`")


if __name__ == "__main__":
    main()
