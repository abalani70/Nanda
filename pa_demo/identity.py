"""Agent identity: keys, AgentFacts records, and signed message envelopes.

Uses Ed25519 (via the `cryptography` package) when available. If it is not
installed, falls back to a clearly-labelled SIMULATED signature so the demo
still runs on any laptop.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    CRYPTO_MODE = "ed25519"
except ImportError:  # pragma: no cover - fallback path
    CRYPTO_MODE = "simulated"


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode())


def canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


class KeyPair:
    """An agent's signing key. Public half is published in its AgentFacts."""

    def __init__(self, seed: bytes | None = None):
        seed = seed or os.urandom(32)
        self._seed = seed
        if CRYPTO_MODE == "ed25519":
            self._priv = Ed25519PrivateKey.from_private_bytes(seed)
            pub = self._priv.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        else:
            self._secret = seed
            pub = hashlib.sha256(self._secret).digest()
        self.public_key = _b64(pub)

    @classmethod
    def load_or_create(cls, path) -> "KeyPair":
        """Persistent key, so the public key published in the NANDA Index stays valid."""
        path = Path(path)
        if path.exists():
            saved = json.loads(path.read_text())
            if saved["alg"] != CRYPTO_MODE:
                raise RuntimeError(
                    f"{path} holds a {saved['alg']} key but signatures are {CRYPTO_MODE}; "
                    f"use the same Python environment you registered with")
            return cls(_unb64(saved["seed"]))
        kp = cls()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"alg": CRYPTO_MODE, "seed": _b64(kp._seed)}, fh)
        return kp

    def sign(self, data: bytes) -> str:
        if CRYPTO_MODE == "ed25519":
            return _b64(self._priv.sign(data))
        # SIMULATED: not real public-key crypto; for offline demo only.
        return _b64(hmac.new(_unb64(self.public_key), data, hashlib.sha256).digest())


def verify(public_key_b64: str, data: bytes, sig_b64: str) -> bool:
    try:
        if CRYPTO_MODE == "ed25519":
            Ed25519PublicKey.from_public_bytes(_unb64(public_key_b64)).verify(
                _unb64(sig_b64), data
            )
            return True
        expected = hmac.new(_unb64(public_key_b64), data, hashlib.sha256).digest()
        return hmac.compare_digest(expected, _unb64(sig_b64))
    except (InvalidSignature if CRYPTO_MODE == "ed25519" else Exception, ValueError):
        return False


def make_agentfacts(agent_id: str, name: str, org: str, role: str,
                    capabilities: list[str], endpoint: str, keys: KeyPair) -> dict:
    """An AgentFacts-style record: who the agent is, what it can do, how to reach it."""
    return {
        "agent_id": agent_id,
        "name": name,
        "organization": org,
        "role": role,
        "capabilities": capabilities,
        "endpoint": endpoint,
        "public_key": keys.public_key,
        "key_alg": CRYPTO_MODE,
        "issued_at": int(time.time()),
        "synthetic": True,
    }


def seal(sender_id: str, recipient_id: str, msg_type: str, body: dict,
         keys: KeyPair, thread_id: str | None = None) -> dict:
    """Create a signed envelope."""
    env = {
        "id": str(uuid.uuid4())[:8],
        "thread_id": thread_id or str(uuid.uuid4())[:8],
        "from": sender_id,
        "to": recipient_id,
        "type": msg_type,
        "ts": time.time(),
        "body": body,
    }
    env["sig"] = keys.sign(canonical(env))
    return env


def check_envelope(env: dict, sender_facts: dict | None) -> tuple[bool, str]:
    """Verify an envelope against the sender's published AgentFacts."""
    if sender_facts is None:
        return False, f"sender {env.get('from')!r} not found in index"
    unsigned = {k: v for k, v in env.items() if k != "sig"}
    if not verify(sender_facts["public_key"], canonical(unsigned), env.get("sig", "")):
        return False, "signature does not match sender's published key"
    return True, "signature verified against AgentFacts"
