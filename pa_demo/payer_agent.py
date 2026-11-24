"""Payer prior-auth agent.

Verifies who is calling (AgentFacts signature), checks the request against a
coverage policy, and returns APPROVED / PENDED (needs more info) / DENIED.
It never denies on its own: proposed denials go to a clinician reviewer.
"""
from __future__ import annotations

import random

from . import console as c
from .identity import KeyPair, check_envelope, make_agentfacts, seal

# Simplified coverage policy for lumbar MRI (illustrative, not a real payer policy)
POLICY = {
    "72148": {
        "name": "MRI lumbar spine",
        "min_conservative_weeks": 6,
        "evidence_doc_type": "pt-notes",
        "criteria_text": "≥6 weeks documented conservative therapy, or red-flag symptoms",
    }
}


class PayerAgent:
    AGENT_ID = "nanda:payer.example-health/prior-auth"

    def __init__(self, index, oversight, endpoint="inproc://payer", agent_id=None, keys=None):
        self.AGENT_ID = agent_id or self.AGENT_ID
        self.keys = keys or KeyPair()
        self.index = index
        self.oversight = oversight
        self.facts = make_agentfacts(
            self.AGENT_ID, "Prior Auth Review Agent", "Example Health Plan", "payer",
            ["prior-auth.review", "fhir.pas"], endpoint, self.keys,
        )
        self.cases: dict[str, dict] = {}

    # ------------------------------------------------------------------ helpers
    def _reply(self, env, msg_type, body):
        return seal(self.AGENT_ID, env["from"], msg_type, body, self.keys, env["thread_id"])

    def _decision(self, env, status, summary, **extra):
        return self._reply(env, "pa.decision", {"status": status, "summary": summary, **extra})

    # ------------------------------------------------------------------ inbound
    def handle(self, env: dict) -> dict:
        sender = self.index.resolve(env.get("from", ""))
        valid, why = check_envelope(env, sender)
        if not valid:
            c.bad(f"payer: identity check failed — {why}")
            self.oversight.alert("Payer security analyst",
                                 f"Rejected message claiming to be {env.get('from')}: {why}")
            return self._reply(env, "pa.rejected",
                               {"status": "REJECTED", "summary": f"identity check failed: {why}"})
        c.ok(f"payer: {why} ({sender['organization']})")
        if "prior-auth.submit" not in sender["capabilities"]:
            return self._reply(env, "pa.rejected",
                               {"status": "REJECTED", "summary": "sender not authorized to submit PA"})

        if env["type"] == "pa.submit":
            self.cases[env["thread_id"]] = {"request": env["body"], "documents": list(env["body"]["documents"])}
        elif env["type"] == "pa.additional-info":
            case = self.cases.get(env["thread_id"])
            if case is None:
                return self._decision(env, "REJECTED", "unknown case")
            case["documents"].extend(env["body"]["documents"])
        else:
            return self._decision(env, "REJECTED", f"unsupported message type {env['type']}")
        return self._evaluate(env)

    # ------------------------------------------------------------------ policy
    def _evaluate(self, env):
        case = self.cases[env["thread_id"]]
        req = case["request"]
        code = req["service_request"]["code"]
        policy = POLICY.get(code)
        if policy is None:
            return self._decision(env, "APPROVED", "no PA required for this service",
                                  auth_number=None)

        if req.get("red_flags"):
            return self._approve(env, "red-flag symptoms present")

        weeks = [d["conservative_therapy_weeks"] for d in case["documents"]
                 if "conservative_therapy_weeks" in d]
        c.step(f"payer: checking criteria — {policy['criteria_text']}")

        if not weeks:
            return self._decision(
                env, "PENDED",
                f"need documentation of conservative therapy ({policy['evidence_doc_type']})",
                requested_items=[{"doc_type": policy["evidence_doc_type"],
                                  "reason": policy["criteria_text"]}],
            )
        best = max(weeks)
        if best >= policy["min_conservative_weeks"]:
            return self._approve(env, f"{best} weeks conservative therapy documented")

        # Agent would deny -> a clinician must make that call (HAL checkpoint)
        reason = (f"Only {best} weeks of conservative therapy documented; "
                  f"policy requires {policy['min_conservative_weeks']}.")
        confirmed = self.oversight.ask(
            "Payer clinician reviewer (MD)",
            f"Agent proposes DENIAL of {policy['name']} for {req['patient']['name']}. "
            f"Type y to CONFIRM the denial, n to OVERTURN it (approve the MRI).",
            reason, key="confirm_denial", default=True,
            labels=("DENIAL CONFIRMED", "DENIAL OVERTURNED"),
        )
        if confirmed:
            return self._decision(env, "DENIED", reason, reason_code="MED-NEC-01",
                                  appeal_rights=True, reviewed_by="clinician")
        return self._approve(env, "clinician override of proposed denial")

    def _approve(self, env, why):
        auth = f"AUTH-{random.randint(100000, 999999)}"
        return self._decision(env, "APPROVED", f"{why}; auth {auth}", auth_number=auth)
