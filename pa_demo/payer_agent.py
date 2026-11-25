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


# What the insurer agent advertises on its A2A Agent Card
A2A_DESCRIPTION = ("Reviews prior-authorization requests for Example Health Plan members. Send a "
                   "signed PA request; the task completes with a decision, or asks for more "
                   "documentation (TASK_STATE_INPUT_REQUIRED). Proposed denials go to a clinician.")
A2A_SKILLS = [{
    "id": "prior-auth.review",
    "name": "Prior authorization review",
    "description": "Checks a signed prior-auth request against coverage policy and returns "
                   "APPROVED, PENDED (send more info) or DENIED (clinician-reviewed).",
    "tags": ["prior-auth", "fhir.pas", "payer", "cms-0057-f"],
    "inputModes": ["application/vnd.halhack.signed-envelope+json"],
    "outputModes": ["application/vnd.halhack.signed-envelope+json", "text/plain"],
}]


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

        thread = env["thread_id"]
        case = self.cases.get(thread)
        if env["type"] == "pa.submit":
            if case is not None:
                return self._decision(env, "REJECTED", "a case with this thread already exists")
            self.cases[thread] = {"sender": env["from"], "request": env["body"],
                                  "documents": list(env["body"]["documents"])}
        elif env["type"] == "pa.additional-info":
            # Only the agent that opened a case may add to it.
            if case is None or case["sender"] != env["from"]:
                return self._decision(env, "REJECTED", "unknown case")
            case["documents"].extend(env["body"]["documents"])
        else:
            return self._decision(env, "REJECTED", f"unsupported message type {env['type']}")
        try:
            return self._evaluate(env)
        except Exception:
            self.forget(thread)   # the case can't continue: don't keep its documents
            raise

    def forget(self, thread_id: str) -> None:
        """Drop a finished case and its clinical documents."""
        self.cases.pop(thread_id, None)

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
