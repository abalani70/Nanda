"""Provider (health system) prior-auth agent.

Discovers the payer's agent in the NANDA Index, reads its A2A Agent Card,
builds a PA request from the EMR, submits it over A2A (SendMessage), and handles the payer's answer — including going back to the EMR
for more documentation when the payer pends the request.
"""
from __future__ import annotations

from . import console as c
from . import emr
from .identity import KeyPair, check_envelope, make_agentfacts, seal

MAX_ROUNDS = 3


class ProviderAgent:
    AGENT_ID = "nanda:provider.riverside-health/prior-auth"

    def __init__(self, index, oversight, transport, agent_id=None, keys=None):
        self.AGENT_ID = agent_id or self.AGENT_ID
        self.keys = keys or KeyPair()
        self.index = index
        self.oversight = oversight
        self.transport = transport
        self.facts = make_agentfacts(
            self.AGENT_ID, "Riverside Health PA Agent", "Riverside Health System", "provider",
            ["prior-auth.submit", "fhir.pas"], "inproc://provider", self.keys,
        )

    def discover_payer(self, payer_org: str) -> dict:
        matches = self.index.find("prior-auth.review", organization=payer_org)
        if not matches:
            raise RuntimeError(f"no prior-auth agent found for {payer_org}")
        payer = matches[0]
        c.ok(f"provider: discovered {payer['agent_id']} via index "
             f"(key {payer['key_alg']}, endpoint {payer['endpoint']})")
        card = self.transport.agent_card(payer["endpoint"])   # A2A discovery: what can it do?
        if not any(s.get("id") == "prior-auth.review" for s in card.get("skills", [])):
            raise RuntimeError(f"{payer['agent_id']}'s Agent Card has no prior-auth.review skill")
        c.ok(f"provider: A2A Agent Card for {card['name']} offers prior-auth.review")
        return payer

    def _build_request(self, patient_id: str) -> dict:
        p = emr.get_patient(patient_id)
        withhold = set(p.get("initially_withhold", []))
        docs = [d for d in p["documents"] if d["type"] not in withhold]
        return {
            "summary": f"PA request: {p['service_request']['display']} for {p['name']}",
            "patient": {"id": patient_id, "name": p["name"], "dob": p["dob"],
                        "member_id": p["member_id"]},
            "conditions": p["conditions"],
            "service_request": p["service_request"],
            "red_flags": p["red_flags"],
            "documents": docs,
        }

    def _send(self, env, payer):
        c.send(env, "Provider agent", "Payer agent")
        reply = self.transport(env, payer["endpoint"])
        c.send(reply, "Payer agent", "Provider agent")   # arrives first, then gets verified
        valid, why = check_envelope(reply, payer)
        if not valid:
            raise RuntimeError(f"payer reply failed verification: {why}")
        return reply

    def request_prior_auth(self, patient_id: str, payer_org: str) -> dict:
        payer = self.discover_payer(payer_org)
        body = self._build_request(patient_id)
        c.step(f"provider: pulled {len(body['documents'])} document(s) from EMR "
               f"(minimum necessary)")
        env = seal(self.AGENT_ID, payer["agent_id"], "pa.submit", body, self.keys)
        thread = env["thread_id"]
        reply = self._send(env, payer)

        for _ in range(MAX_ROUNDS):
            status = reply["body"].get("status")
            if status != "PENDED":
                return reply["body"]
            c.outcome("PENDED", reply["body"]["summary"])
            docs = []
            for item in reply["body"].get("requested_items", []):
                found = emr.find_documents(patient_id, item["doc_type"])
                c.step(f"provider: searched EMR for {item['doc_type']} → {len(found)} found")
                docs.extend(found)
            if not docs:
                return {"status": "PENDED", "summary": "requested documentation not in chart"}
            released = self.oversight.ask(
                "Provider UM coordinator",
                f"Release {len(docs)} additional clinical document(s) to {payer['organization']}?",
                "; ".join(f"{d['type']} {d['date']}: {d['text']}" for d in docs),
                key="release_phi", default=True,
            )
            if not released:
                return {"status": "PENDED", "summary": "staff declined to release documents"}
            env = seal(self.AGENT_ID, payer["agent_id"], "pa.additional-info",
                       {"summary": f"additional info: {len(docs)} document(s)", "documents": docs},
                       self.keys, thread)
            reply = self._send(env, payer)
        return reply["body"]


class SpoofedProvider:
    """A rogue agent that claims to be Riverside's agent but holds a different key."""

    def __init__(self, transport, claimed_id=ProviderAgent.AGENT_ID):
        self.keys = KeyPair()  # NOT the key published in Riverside's AgentFacts
        self.transport = transport
        self.claimed_id = claimed_id

    def attack(self, payer: dict) -> dict:
        body = {
            "summary": "PA request: MRI lumbar spine (forged clinical note)",
            "patient": {"id": "pt-9999", "name": "Unknown", "dob": "1980-01-01",
                        "member_id": "HLT-00000"},
            "conditions": [{"code": "M54.16", "system": "ICD-10"}],
            "service_request": {"code": "72148", "system": "CPT", "display": "MRI lumbar spine"},
            "red_flags": ["fabricated"],
            "documents": [{"type": "pt-notes", "text": "forged", "conservative_therapy_weeks": 12}],
        }
        if hasattr(self.transport, "agent_card"):
            self.transport.agent_card(payer["endpoint"])   # the card is public: attackers read it too
        env = seal(self.claimed_id, payer["agent_id"], "pa.submit", body, self.keys)
        c.send(env, "Rogue agent (claims Riverside)", "Payer agent")
        reply = self.transport(env, payer["endpoint"])
        c.send(reply, "Payer agent", "Rogue agent")
        return reply["body"]
