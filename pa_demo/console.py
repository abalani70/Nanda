"""Readable console output: who is contacting whom, and why."""
import json
import os
import sys
import time

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
PAUSE = float(os.environ.get("DEMO_PAUSE", "0"))

# Optional observers (e.g. the web watchtower). Each gets (kind, data) per event.
_listeners = []


def subscribe(fn):
    _listeners.append(fn)


def _emit(kind, **data):
    for fn in _listeners:
        fn(kind, data)


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
    _emit("step", text=msg)
    print(_c("2", f"  · {msg}"))


def send(env, frm, to):
    _emit("send", env=env, frm=frm, to=to)
    body = env["body"]
    summary = body.get("summary") or env["type"]
    print(f"  {_c('1;34', frm)} {_c('1', '──▶')} {_c('1;35', to)}  "
          f"[{_c('33', env['type'])}] {summary}")
    _pause()


def ok(msg):
    _emit("ok", text=msg)
    print(_c("32", f"    ✔ {msg}"))


def bad(msg):
    _emit("bad", text=msg)
    print(_c("31", f"    ✘ {msg}"))


def human(who, question, detail):
    _emit("human", who=who, question=question, detail=detail)
    print(_c("1;43;30", f"  👤 HUMAN CHECKPOINT — {who}"))
    print(f"    {question}")
    if detail:
        print(_c("2", f"    {detail}"))
    _pause()


def human_decision(who, word):
    _emit("human_decision", who=who, word=word)
    print(_c("1;33", f"    → {who}: {word}"))


def alert(who, message):
    _emit("alert", who=who, message=message)
    print(_c("1;41;37", f"  🚨 ALERT to {who}") + f" {message}")


def outcome(status, detail=""):
    _emit("outcome", status=status, detail=detail)
    color = {"APPROVED": "1;42;30", "DENIED": "1;41;37", "PENDED": "1;43;30",
             "REJECTED": "1;41;37"}.get(status, "1")
    print("  " + _c(color, f" {status} ") + (f"  {detail}" if detail else ""))


def prompt(s):
    return _c("1;33", s)


def dump(obj):
    print(_c("2", json.dumps(obj, indent=2)))
