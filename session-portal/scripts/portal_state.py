#!/usr/bin/env python3
"""portal_state.py — conservative, READ-ONLY session-state classification.

Answers one question for the portal: is it safe to hand a message to a given session
right now? It reads the product's own append-only session log (never writes it, never
opens a second writer) plus optional out-of-band hints (is the process alive?), and maps
the evidence to one of seven states:

    active            recent log writes -> the session is working; do NOT deliver
    idle              an explicit end-of-turn marker is the latest signal and the log
                      has been quiet a moment -> safe to deliver at a boundary
    waiting-for-user  the latest signal is an approval/input request
    completed         proven closed (explicit completion + process gone)
    unavailable       no log / product not installed
    stale             very old and unproven -> treat as not deliverable
    unknown           quiet but with NO completion marker -> idleness is NOT proven

The load-bearing rule from issue #16: *a quiet transcript does not prove idleness.*
So a log that simply stopped, with no explicit turn-complete marker, is `unknown`
(which the delivery layer treats as "queue, don't push"), never `idle`.

Log locations mirror monitor-agent-thread's surfaces (Claude:
~/.claude/projects/**/<session-id>.jsonl, Codex:
~/.codex/sessions/**/rollout-*-<thread-id>.jsonl); both roots are env-overridable for
hermetic tests. stdlib only; Windows-safe.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterable

# A session identifier is an opaque token, never a filesystem path. Anything with path
# separators, parent refs, env-var/home sigils, or a null byte is refused so resolve_log
# can't be turned into an arbitrary-file read (threat-model: "no arbitrary file reads").
_SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")

# States
ACTIVE = "active"
IDLE = "idle"
WAITING = "waiting-for-user"
COMPLETED = "completed"
UNAVAILABLE = "unavailable"
STALE = "stale"
UNKNOWN = "unknown"

ACTIVE_WINDOW_SECONDS = 120
STALE_WINDOW_SECONDS = 6 * 3600

_AWAIT_TOOLS = {"askuserquestion", "request_user_input", "permissionprompt", "exitplanmode"}
_AWAIT_RE = ("approval required", "input required", "waiting for user", "permission denied",
             "awaiting your", "needs your input")


def _now() -> float:
    return time.time()


def claude_root() -> Path:
    env = os.environ.get("SESSION_PORTAL_CLAUDE_LOGS")
    return Path(env).resolve() if env else (Path.home() / ".claude" / "projects").resolve()


def codex_root() -> Path:
    env = os.environ.get("SESSION_PORTAL_CODEX_LOGS")
    return Path(env).resolve() if env else (Path.home() / ".codex" / "sessions").resolve()


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(item, dict):
                yield item


def _tail(path: Path, limit: int = 400) -> list[dict[str, Any]]:
    rows: deque[dict[str, Any]] = deque(maxlen=limit)
    for row in _iter_jsonl(path):
        rows.append(row)
    return list(rows)


def resolve_log(product: str, session: str) -> Path | None:
    """Find the session's log by id substring WITHIN the product's log root. None if not
    found. The identifier is validated as an opaque token (no path chars), so this can only
    ever match a file already under the product root — never an arbitrary path. Read-only:
    it locates and stat/reads a log, never creates or modifies one, and never expands env
    vars or `~` from the identifier."""
    if not isinstance(session, str) or not _SAFE_SESSION_RE.match(session):
        return None
    if product == "claude":
        root = claude_root()
        if not root.exists():
            return None
        cands = [p for p in root.rglob("*.jsonl")
                 if "subagents" not in {part.lower() for part in p.parts}
                 and session.lower() in str(p).lower()]
    elif product == "codex":
        root = codex_root()
        if not root.exists():
            return None
        cands = [p for p in root.rglob("rollout-*.jsonl") if session.lower() in str(p).lower()]
    else:
        return None
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0].resolve()


def _analyze_claude(rows: list[dict[str, Any]]) -> dict[str, Any]:
    turn_complete = False
    awaiting = False
    for row in rows:
        msg = row.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        # A fresh USER message after a completion marker starts a NEW turn: the session is
        # re-engaged and about to work. Without this reset a stale end_turn would keep the
        # session classified idle even though the user just prompted it again.
        if role == "user":
            turn_complete = False
            awaiting = False
            continue
        stop = msg.get("stop_reason")
        content = msg.get("content")
        # Any assistant tool activity after a stop resets completion.
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use":
                    name = str(part.get("name") or "").lower()
                    turn_complete = False
                    awaiting = name in _AWAIT_TOOLS
        if stop == "end_turn":
            turn_complete = True
            awaiting = False
        elif stop == "tool_use":
            turn_complete = False
    return {"turn_complete": turn_complete, "awaiting_input": awaiting}


def _analyze_codex(rows: list[dict[str, Any]]) -> dict[str, Any]:
    turn_complete = False
    awaiting = False
    complete_idx = -1
    activity_idx = -1
    for i, row in enumerate(rows):
        kind = row.get("type")
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if kind == "event_msg":
            et = payload.get("type")
            if et == "task_complete":
                complete_idx = i
            elif et == "agent_message":
                activity_idx = i
                text = str(payload.get("message") or "").lower()
                awaiting = any(k in text for k in _AWAIT_RE)
            elif et in ("user_message", "user_input"):
                # A fresh user turn after a task_complete re-engages the session.
                activity_idx = i
                awaiting = False
        elif kind == "response_item":
            it = payload.get("type")
            if it in ("custom_tool_call", "function_call"):
                activity_idx = i
                awaiting = False
            elif it == "message" and payload.get("role") == "user":
                activity_idx = i
                awaiting = False
    turn_complete = complete_idx >= 0 and complete_idx >= activity_idx
    return {"turn_complete": turn_complete, "awaiting_input": awaiting}


def classify(product: str, session: str, *, process_alive: bool | None = None,
             now: float | None = None) -> dict[str, Any]:
    """Classify a session's deliverability state from read-only evidence.

    process_alive: optional out-of-band hint. True/False sharpen idle vs completed;
    None (unknown) stays conservative (never upgrades to `completed`).
    """
    ts = now if now is not None else _now()
    log = resolve_log(product, session)
    if log is None:
        return {"state": UNAVAILABLE, "reason": "no session log found", "log": None,
                "age_seconds": None, "evidence": {}}
    try:
        age = max(0.0, ts - log.stat().st_mtime)
    except OSError:
        return {"state": UNAVAILABLE, "reason": "log stat failed", "log": str(log),
                "age_seconds": None, "evidence": {}}
    rows = _tail(log)
    ev = _analyze_claude(rows) if product == "claude" else _analyze_codex(rows)

    if ev["awaiting_input"] and age < STALE_WINDOW_SECONDS:
        state, reason = WAITING, "latest signal is an approval/input request"
    elif ev["turn_complete"]:
        if process_alive is False:
            state, reason = COMPLETED, "explicit turn-complete and process not alive"
        elif age >= STALE_WINDOW_SECONDS:
            state, reason = STALE, "turn-complete but log is very old (closure unproven)"
        else:
            state, reason = IDLE, "explicit turn-complete marker and log quiet"
    elif age < ACTIVE_WINDOW_SECONDS:
        state, reason = ACTIVE, "recent log activity"
    elif age >= STALE_WINDOW_SECONDS:
        state, reason = STALE, "log is very old"
    else:
        # Quiet, but NO explicit completion marker: idleness is not proven.
        state, reason = UNKNOWN, "quiet transcript without a completion marker (idleness unproven)"

    return {"state": state, "reason": reason, "log": str(log),
            "age_seconds": round(age, 1), "evidence": ev}


PUSH_DELIVERABLE = {IDLE, WAITING}


def is_push_deliverable(state: str) -> bool:
    return state in PUSH_DELIVERABLE
