# Prior Auth, Agent-to-Agent — HALHack demo

A provider agent submits a prior-authorization request built from EMR data to a payer
agent at a different organization, over the **A2A (Agent2Agent) protocol v1.0**. The payer verifies who is calling, checks policy,
and answers **APPROVED**, **PENDED (send more info)**, or **DENIED**. Humans only see
the decisions that need judgment (HAL 2 — one human supervising a team of agents).

All patient data is synthetic. The coverage policy is illustrative.

## Run it (Python 3.9+)

```bash
pip install cryptography        # optional: real Ed25519 signatures (falls back to simulated)
python run_demo.py              # all 4 scenarios, scripted humans
```

| Option | What it does |
|---|---|
| `--interactive` | You play the humans (y/n prompts) — best for a live demo |
| `--http` | Payer agent is a real A2A server over HTTP (Agent Card + JSON-RPC at `http://127.0.0.1:8765/a2a`) |
| `--case B` | Run one scenario (A, B, C, S; repeatable) |
| `--clinician-overrides` | Scripted clinician overturns the proposed denial in C |
| `--mock-index` | Offline: in-memory `MockIndex` instead of the live NANDA Index |
| `--register` | Publish/update both agents' AgentFacts in the NANDA Index, then exit |
| `DEMO_PAUSE=0.8` | Env var: slow output down for an audience |

## Web watchtower (graphical demo)

```bash
python web_app.py            # then open http://127.0.0.1:8080
python web_app.py --live-index   # discover/verify through api.nandaindex.org instead of MockIndex
```

The API has no login, so the server only binds to loopback; add `--allow-remote` with
`--host 0.0.0.0` to show it on another device on a trusted network. If no one answers a
checkpoint within 15 minutes the task stops with an error: nothing is released or decided
on a person's behalf.

Same agents, shown live in the browser. The insurer agent runs as a real A2A server on
`http://127.0.0.1:8765` (move it with `--a2a-port`); its Agent Card is at
`/.well-known/agent-card.json`. The top band is the **agent layer**: the provider agent,
the insurer agent, the **NANDA Index** (the "DNS for agents") and a **spoofed agent**. Every A2A
call (Agent Card fetch, `SendMessage`, the task state that comes back) moves across the graph as a packet, and each agent shows how many it sent and received. The
bottom band is the **human oversight layer**: the provider overseer delegates a task and approves PHI
release, a clinician decides any proposed denial, and a security analyst gets identity alerts.
Checkpoints show up as buttons on the page (or switch to "Scripted humans").

Everything is on one screen: **provider overseer** on the left, the animated graph in the middle, **insurer oversight** on the right.

- Provider overseer (left): pick a synthetic patient, **Delegate to provider agent**, then watch the task's progress track.
- Insurer oversight (right): what the insurer agent received, clinician decisions, security alerts, and a link to its Agent Card.
- Transaction log (under the graph): every signed A2A message, newest first: time, task, sender → receiver,
  message type and A2A operation or task state, decision, the key that signed it, the key the NANDA Index
  publishes for that agent ID, and whether verification passed (genuine / forged / altered / unknown agent).
  A spoof shows a different signer key (forged) or no published key. Click a row to select its task.
- Spoofed agent (second tab on the left, next to Provider overseer): **Impersonate Riverside** (wrong key) or **Unregistered agent** (not in the index); both are rejected. The tab shows the attacker's agent ID and the public key it actually signed with, and every attempt. While you are on it, the Provider tab flags any approval waiting for you.
- `/api/trace` (the **trace.json** button) gives every signed envelope and raw A2A JSON-RPC request/response.

## Scenarios

| | Patient | Flow | Human touch |
|---|---|---|---|
| A | pt-1001 | Full documentation → **APPROVED** | none |
| B | pt-1002 | PT notes withheld (minimum necessary) → **PENDED** → provider agent finds them in EMR → staff approves release → **APPROVED** | provider UM coordinator |
| C | pt-1003 | 0 weeks therapy → agent proposes **DENIAL** → clinician confirms or overturns | payer clinician (MD) |
| S | — | Rogue agent claims to be the provider but signs with the wrong key → **REJECTED** | payer security alert |

## How it maps to NANDA / HAL / A2A

- **A2A v1.0** → `pa_demo/a2a.py` (stdlib only), JSON-RPC binding:
  - The payer agent is an A2A **server**: Agent Card at `/.well-known/agent-card.json` (skill
    `prior-auth.review`, `supportedInterfaces` → JSONRPC 1.0), and `SendMessage` / `GetTask`.
  - The provider (and the spoofed agent) are A2A **clients**: they read the Agent Card, then send
    `SendMessage` with the `A2A-Version: 1.0` header.
  - Decisions map to task states: APPROVED/DENIED → `TASK_STATE_COMPLETED` (decision also in an
    artifact), PENDED → `TASK_STATE_INPUT_REQUIRED` (the provider continues the **same task** by
    `taskId` with the extra documents), identity failure → `TASK_STATE_REJECTED`.
  - Each A2A message carries the signed envelope as a data part
    (`application/vnd.halhack.signed-envelope+json`). A2A has no message signing of its own, so the
    receiver still checks the sender's key in the NANDA Index: the index says *who* an agent is,
    the Agent Card says *how to talk to it*.
  - Without `--http` the CLI sends the same A2A JSON-RPC frames in-process (offline-safe).

- **NANDA Index** → `pa_demo/nanda_index.py`: client for `api.nandaindex.org`
  (search → resolve → index record). `--mock-index` swaps in `pa_demo/index.py` for offline runs.
- **AgentFacts** → `identity.make_agentfacts`: org, role, capabilities, endpoint, public key.
- **Signed envelopes** → every message is Ed25519-signed; the receiver verifies against the
  sender's published key (both directions).
- **HAL checkpoints** → `pa_demo/oversight.py`: denials need a clinician, PHI release needs staff,
  identity failures alert a human.
- **Trace** → `trace.json`: every signed message, every A2A request/response, human touches, outcomes.

## Using the live NANDA Index

By default the demo discovers and verifies agents through the public NANDA Index
(`https://api.nandaindex.org`, open source: `projnanda/nanda-index-v2`). Each agent is
one index entry on its own subdomain, identified by an AIR URN such as
`urn:air:payer.<your-domain>:agent:prior-auth`. Its AgentFacts (capabilities, endpoint,
Ed25519 public key) are published in the entry's extensions.

Two ways to give each agent a verified identity:

- **By email (no DNS):** each agent gets its own mailbox and a *personal* entry,
  `urn:air:host39.org:personal:<mailbox>`. Register with
  `python run_demo.py --register --payer-email <mailbox> --provider-email <mailbox>`, then confirm the
  "Verify your NANDA Index registration" link the index emails to each mailbox. (This repo's agents are
  registered this way, as `halhackpapayer@inkboxmail.com` and `halhackpaprovider@inkboxmail.com`.)
- **By domain:** the steps below.

AgentFacts are published under the entry's `extensions["org.projectnanda"]["agentfacts"]`, which every
public read (including `resolve`) returns.

One-time setup (domain):

1. Create and verify an account at https://nandaindex.org.
2. Pick a domain whose DNS you control, e.g. `halhack.yourdomain.com`.
3. `python run_demo.py --register --domain halhack.yourdomain.com`
   (asks for your login, or set `NANDA_EMAIL` / `NANDA_PASSWORD` / `NANDA_TOKEN`).
   It prints two `_nanda-challenge.…` DNS TXT records.
4. Add those TXT records, then run `python run_demo.py --register` again. Entries stay
   hidden from public lookups until the domains are verified.
5. `python run_demo.py` now runs against the live index.

Keys are stored in `~/.pa-demo/keys/` (outside the repo) and settings in `~/.pa-demo/index.json`.
Register with the same `--http` setting you will demo with, because the endpoint is part of the
published AgentFacts. The web watchtower always runs the payer as an A2A server on
`http://127.0.0.1:8765/a2a`, so for `web_app.py --live-index` register with `--http`. If the entries are missing or don't match the local keys, the demo stops with
a message. On stage without wifi, use `--mock-index`.

Env overrides: `NANDA_INDEX_URL` (e.g. a self-hosted index), `NANDA_DOMAIN`, `NANDA_ORG_PREFIX`
(default `halhack-pa`; org IDs must be globally unique), `NANDA_CONTACT_EMAIL`.

## 90-second demo script

1. "Under CMS-0057-F, payers must offer a prior-auth API by Jan 1, 2027. Here it is agent-to-agent, across two orgs."
2. Run `DEMO_PAUSE=0.6 python run_demo.py --interactive` (add `--mock-index` if offline).
3. **Worked:** identity verification + humans only on denials and PHI release.
   **Didn't / next:** real FHIR (Da Vinci PAS) payloads, agents hosted on public endpoints, a real payer–provider pilot.

## Files

```
run_demo.py               entry point and scenarios
web_app.py                web watchtower server (stdlib http.server)
web/index.html            watchtower page (no external assets; works offline)
pa_demo/identity.py       keys, AgentFacts, signed envelopes
pa_demo/nanda_index.py    NANDA Index API client + registration
pa_demo/index.py          mock NANDA Index (--mock-index)
pa_demo/emr.py            synthetic EMR (3 patients)
pa_demo/provider_agent.py provider agent + spoofed agent
pa_demo/payer_agent.py    payer agent + coverage policy
pa_demo/oversight.py      human checkpoints
pa_demo/a2a.py            A2A v1.0 server + client (JSON-RPC, Agent Card, tasks)
pa_demo/console.py        readable output
```
