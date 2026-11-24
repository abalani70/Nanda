"""Readable console output: who is contacting whom, and why."""
import json
import os
import sys
import time

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
PAUSE = float(os.environ.get("DEMO_PAUSE", "0"))


def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def _pause():
    if PAUSE:
        time.sleep(PAUSE)


def banner(title):
    line = "=" * 72
    print("\n" + _c("1;36", line))
    print(_c("1;36", f"  {title}"))
    print(_c("1;36", line))


def step(msg):
    print(_c("2", f"  · {msg}"))


def send(env, frm, to):
    body = env["body"]
    summary = body.get("summary") or env["type"]
    print(f"  {_c('1;34', frm)} {_c('1', '──▶')} {_c('1;35', to)}  "
          f"[{_c('33', env['type'])}] {summary}")
    _pause()


def ok(msg):
    print(_c("32", f"    ✔ {msg}"))


def bad(msg):
    print(_c("31", f"    ✘ {msg}"))


def human(who, question, detail):
    print(_c("1;43;30", f"  👤 HUMAN CHECKPOINT — {who}"))
    print(f"    {question}")
    if detail:
        print(_c("2", f"    {detail}"))
    _pause()


def human_decision(who, word):
    print(_c("1;33", f"    → {who}: {word}"))


def alert(who, message):
    print(_c("1;41;37", f"  🚨 ALERT to {who}") + f" {message}")


def outcome(status, detail=""):
    color = {"APPROVED": "1;42;30", "DENIED": "1;41;37", "PENDED": "1;43;30",
             "REJECTED": "1;41;37"}.get(status, "1")
    print("  " + _c(color, f" {status} ") + (f"  {detail}" if detail else ""))


def prompt(s):
    return _c("1;33", s)


def dump(obj):
    print(_c("2", json.dumps(obj, indent=2)))
