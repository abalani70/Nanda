# Context for Claude Code

HALHack demo for the NANDA Human-Agent fLow (HAL) Kickoff (Google Mountain View, Nov 20 2026).
Theme: "Who Watches the Swarm?" — keep humans in the loop only for decisions needing judgment.

- Pure Python stdlib + optional `cryptography`. Keep it dependency-light; it must run on a fresh laptop.
- Run: `python run_demo.py` (see README for flags). Always re-run all scenarios after changes.
- Data is synthetic; never add real PHI.
- Likely on-site tasks: swap `MockIndex` for the real NANDA Index (nandaindex.org quickstart),
  connect to Nanda Town via its A2A endpoint guide, convert payloads to FHIR Da Vinci PAS shapes,
  or add a simple web "watchtower" view over `trace.json`.
- Demo should end by answering: what worked, what didn't, what's next.
