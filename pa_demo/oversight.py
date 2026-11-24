"""Human-in-the-loop checkpoints (HAL 2: one human supervising a team of agents).

Humans only see the decisions that genuinely need judgment:
  * payer side: any proposed DENIAL goes to a clinician reviewer
  * provider side: releasing additional PHI requires staff approval
  * security: identity failures are flagged to a human
"""
from __future__ import annotations

from . import console as c


class Oversight:
    def __init__(self, interactive: bool = False, scripted: dict | None = None):
        self.interactive = interactive
        self.scripted = scripted or {}
        self.touches: list[dict] = []

    def ask(self, who: str, question: str, detail: str, key: str, default: bool,
            labels: tuple[str, str] = ("APPROVED", "REJECTED")) -> bool:
        c.human(who, question, detail)
        if self.interactive:
            ans = input(c.prompt("    agree? [y/n] ")).strip().lower()
            decision = ans.startswith("y") if ans else default
        else:
            decision = self.scripted.get(key, default)
            c.human_decision(who, labels[0] if decision else labels[1])
        self.touches.append({"who": who, "question": question, "approved": decision})
        return decision

    def alert(self, who: str, message: str) -> None:
        c.alert(who, message)
        self.touches.append({"who": who, "question": message, "approved": None})
