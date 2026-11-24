"""Mock NANDA Index: a registry that resolves agent IDs to AgentFacts.

Swap `MockIndex.resolve` / `find` for calls to the real NANDA Index
(nandaindex.org) when you wire this up on site.
"""
from __future__ import annotations


class MockIndex:
    def __init__(self):
        self._records: dict[str, dict] = {}

    def register(self, facts: dict) -> None:
        self._records[facts["agent_id"]] = facts

    def resolve(self, agent_id: str) -> dict | None:
        return self._records.get(agent_id)

    def find(self, capability: str, organization: str | None = None) -> list[dict]:
        return [
            f for f in self._records.values()
            if capability in f["capabilities"]
            and (organization is None or f["organization"] == organization)
        ]

    def all(self) -> list[dict]:
        return list(self._records.values())
