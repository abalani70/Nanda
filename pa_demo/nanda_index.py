"""Client for the NANDA Index API (https://api.nandaindex.org, projnanda/nanda-index-v2).

How the real index is shaped, and how the demo maps onto it:

* The index lists *entries* (one per org_id), each identified by an AIR URN and
  anchored either to a verified domain (``urn:air:payer.example.org:agent:prior-auth``,
  proven with a DNS TXT record) or to a verified email address ("personal" entries,
  ``urn:air:host39.org:personal:<email>``, proven by a link emailed to that address).
  Each demo agent gets its own entry: its own subdomain, or its own mailbox.
* The agent's AgentFacts (capabilities, endpoint, Ed25519 public key) are published in
  the entry's ``extensions["org.projectnanda"]["agentfacts"]``. Extensions appear on
  every public read; ``entry_data`` does not (the public entry shows ``url`` instead),
  and entries without a domain may only write under the ``org.projectnanda`` namespace.
* Discovery:  ``GET /api/v1/search?q=<capability>``  → candidate entries
  Resolution: ``GET /api/v1/resolve?locator=<urn>``  → entry.extensions…agentfacts
* Registration (needs a verified nandaindex.org account):
  ``POST /auth/login`` → bearer token; ``POST /api/v1/orgs`` (or ``PUT`` to update), then
  domain entries: ``POST /api/v1/orgs/{id}/domain-challenge`` → TXT record → ``verify-domain``;
  personal entries: confirm the link the index emails (``POST /api/v1/verify-email``).
  Entries stay ``pending`` (invisible to public reads) until verified.

Stdlib only (urllib). Same read interface as ``MockIndex``: resolve / find / all.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://api.nandaindex.org"
STATE_DIR = Path(os.environ.get("PA_DEMO_HOME", Path.home() / ".pa-demo"))
CONFIG_FILE = STATE_DIR / "index.json"
DEMO_TAG = "halhack-pa-demo"
NANDA_EXT = "org.projectnanda"
USER_AGENT = "pa-agents-demo/1.0 (+https://github.com/projnanda/nanda-index-v2)"


class IndexAPIError(RuntimeError):
    def __init__(self, status: int, error: str, detail: str):
        super().__init__(f"{status} {error}: {detail}")
        self.status, self.error, self.detail = status, error, detail


# --------------------------------------------------------------------- config
def load_config() -> dict:
    """Index settings: env vars override the file written by `run_demo.py --register`."""
    cfg = {}
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text())
    cfg["base_url"] = os.environ.get("NANDA_INDEX_URL", cfg.get("base_url", DEFAULT_BASE_URL)).rstrip("/")
    cfg["domain"] = os.environ.get("NANDA_DOMAIN", cfg.get("domain", ""))
    cfg["org_prefix"] = os.environ.get("NANDA_ORG_PREFIX", cfg.get("org_prefix", "halhack-pa"))
    emails = dict(cfg.get("emails") or {})
    for role in ("payer", "provider"):
        if os.environ.get(f"NANDA_{role.upper()}_EMAIL"):
            emails[role] = os.environ[f"NANDA_{role.upper()}_EMAIL"]
    cfg["emails"] = {r: e.strip().lower() for r, e in emails.items() if e.strip()}
    return cfg


def save_config(cfg: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(
        {k: cfg[k] for k in ("base_url", "domain", "org_prefix", "emails")}, indent=2))


def agent_identity(cfg: dict, role: str) -> dict:
    """org_id and AIR URN for the demo's payer/provider agent (email- or domain-anchored)."""
    org_id = f"{cfg['org_prefix']}-{role}"
    email = cfg.get("emails", {}).get(role)
    if email:
        local, _, host = email.partition("@")
        card = os.environ.get(f"NANDA_{role.upper()}_CARD_URL") or (
            f"https://inkbox.ai/a2a/{local}/card" if host == "inkboxmail.com"
            else f"https://{host}/.well-known/agent-card.json")
        return {"kind": "personal", "org_id": org_id, "email": email, "card_url": card,
                "agent_id": f"urn:air:host39.org:personal:{email}"}
    if not cfg.get("domain"):
        raise RuntimeError(
            "no NANDA identity configured — set NANDA_PAYER_EMAIL/NANDA_PROVIDER_EMAIL (mailboxes "
            "you can read) or NANDA_DOMAIN (a domain whose DNS you control), then run "
            "`python run_demo.py --register`, or use --mock-index")
    domain = f"{role}.{cfg['domain']}"
    return {"kind": "domain", "org_id": org_id, "domain": domain,
            "agent_id": f"urn:air:{domain}:agent:prior-auth"}


# --------------------------------------------------------------------- client
class NandaIndexClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: str | None = None, timeout: float = 10,
                 cache_ttl: float | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.cache_ttl = cache_ttl                 # None: cache for the whole run (CLI)
        self._facts: dict[str, dict | None] = {}   # agent_id -> AgentFacts (per-run cache)
        self._fetched: dict[str, float] = {}

    # ---------------------------------------------------------------- http
    def _call(self, method: str, path: str, body: dict | None = None,
              query: dict | None = None, auth: bool = False) -> dict:
        url = self.base_url + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if auth:
            if not self.token:
                raise RuntimeError("this call needs a NANDA Index login (see --register)")
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}")
            except ValueError:
                err = {}
            raise IndexAPIError(e.code, err.get("error", e.reason), err.get("detail", "")) from None

    def health(self) -> dict:
        return self._call("GET", "/health")

    # ---------------------------------------------------------------- reads
    def record(self, org_id: str) -> dict | None:
        """Public index record for an active entry (None if missing/pending/suspended)."""
        try:
            return self._call("GET", f"/api/v1/index/{urllib.parse.quote(org_id)}")
        except IndexAPIError as e:
            if e.status == 404:
                return None
            raise

    def resolve(self, agent_id: str) -> dict | None:
        """AIR URN -> AgentFacts, or None if the index has no active entry for it."""
        if agent_id in self._facts and (
                self.cache_ttl is None or time.time() - self._fetched[agent_id] < self.cache_ttl):
            return self._facts[agent_id]
        facts = None
        if agent_id.startswith("urn:air:"):
            try:
                res = self._call("GET", "/api/v1/resolve", query={"locator": agent_id})
            except IndexAPIError as e:
                if e.status not in (400, 404):
                    raise
                res = None
            if res and res.get("match") == "exact":
                facts = _facts_from_entry(res.get("entry"), agent_id)
        self._facts[agent_id] = facts
        self._fetched[agent_id] = time.time()
        return facts

    def find(self, capability: str, organization: str | None = None) -> list[dict]:
        """Discover agents advertising a capability (tag search, then resolve each hit)."""
        hits: dict[str, dict] = {}
        for q in (capability, DEMO_TAG):   # the index's keyword search may tokenize "a.b"
            for hit in self._call("GET", "/api/v1/search", query={"q": q}).get("results", []):
                hits.setdefault(hit["identifier"], hit)
        out = []
        for hit in hits.values():
            if capability not in hit.get("tags", []) or hit.get("status") != "active":
                continue
            facts = self.resolve(hit["identifier"])
            if (facts and capability in facts.get("capabilities", [])
                    and (organization is None or facts.get("organization") == organization)):
                out.append(facts)
        return out

    def all(self) -> list[dict]:
        """AgentFacts resolved from the index during this run."""
        return [f for f in self._facts.values() if f]

    # ---------------------------------------------------------------- writes
    def login(self, email: str, password: str) -> None:
        self.token = self._call("POST", "/auth/login", {"email": email, "password": password})["token"]

    def register_agent(self, facts: dict, ident: dict, contact_email: str) -> dict:
        """Create (or update) the index entry carrying this agent's AgentFacts."""
        fields = {
            "display_name": f"{facts['organization']} PA agent (fictional HALHack demo)",
            "description": (f"Synthetic {facts['role']} prior-authorization agent for the NANDA "
                            f"HALHack demo. Not a real organization; no real patient data."),
            "tags": [DEMO_TAG, "synthetic", "prior-auth", *facts["capabilities"]][:20],
            "extensions": {NANDA_EXT: {"agentfacts": facts}},
        }
        if ident["kind"] == "personal":
            # Identity is the mailbox itself: the index emails it a verification link.
            create = {
                "org_id": ident["org_id"],
                "hosting_path": "personal",
                "identifier": ident["agent_id"],
                "contact_email": ident["email"],
                "registry_url": ident["card_url"],
                "media_type": "application/a2a-agent-card+json",
                **fields,
            }
        else:
            create = {
                "org_id": ident["org_id"],
                "hosting_path": "registry",
                "domain": ident["domain"],
                "identifier": ident["agent_id"],
                "contact_email": contact_email,
                "registry_url": f"https://{ident['domain']}/.well-known/agentfacts.json",
                **fields,
            }
        try:
            return self._call("POST", "/api/v1/orgs", create, auth=True)
        except IndexAPIError as e:
            # The server reports an existing entry as an identifier or org_id conflict.
            if not (e.status == 409 and ("org_id" in e.detail or "identifier" in e.detail)):
                raise
        # Already registered (by us, hopefully): update in place, e.g. after key rotation.
        try:
            return self._call("PUT", f"/api/v1/orgs/{ident['org_id']}", fields, auth=True)
        except IndexAPIError as e:
            if e.status in (403, 404):
                raise RuntimeError(
                    f"org_id {ident['org_id']!r} is taken by another account — "
                    f"set NANDA_ORG_PREFIX to something unique") from None
            raise

    def domain_challenge(self, org_id: str) -> dict:
        return self._call("POST", f"/api/v1/orgs/{org_id}/domain-challenge", {}, auth=True)

    def verify_domain(self, org_id: str) -> dict:
        return self._call("POST", f"/api/v1/orgs/{org_id}/verify-domain", {}, auth=True)

    def own_record(self, org_id: str) -> dict:
        return self._call("GET", f"/api/v1/orgs/{org_id}", auth=True)


def _facts_from_entry(entry: dict | None, agent_id: str) -> dict | None:
    """Pull AgentFacts out of a public index entry, refusing facts that claim a different ID."""
    nanda = ((entry or {}).get("extensions") or {}).get(NANDA_EXT) or {}
    if nanda.get("status") != "active" or (entry or {}).get("identifier") != agent_id:
        return None
    facts = nanda.get("agentfacts")
    if not isinstance(facts, dict):
        return None
    # Public reads re-case nested keys to camelCase (public_key -> publicKey); undo that.
    facts = {re.sub(r"(?<!^)([A-Z])", r"_\1", k).lower(): v for k, v in facts.items()}
    if facts.get("agent_id") != agent_id:
        return None
    return facts


# Fields that must match between what an agent holds locally and what the index publishes.
PUBLISHED_FIELDS = ("agent_id", "organization", "capabilities", "endpoint", "public_key", "key_alg")


def facts_mismatch(local: dict, published: dict | None) -> str | None:
    if published is None:
        return "no active index entry"
    diff = [k for k in PUBLISHED_FIELDS if local.get(k) != published.get(k)]
    return f"index entry's {', '.join(diff)} do(es) not match the local agent" if diff else None
