#!/usr/bin/env python3
"""Safely discover and summarize Claude Code or Codex JSONL session logs.

The output deliberately excludes reasoning, prompts, raw tool arguments, configuration,
tokens, signatures, and encrypted content.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

HOME = Path.home()
CLAUDE_ROOT = HOME / ".claude" / "projects"
CODEX_ROOT = HOME / ".codex" / "sessions"
ALERT_RE = re.compile(
    r"\b(permission denied|approval required|input required|timed out|blocked by|cannot continue|failed to|fatal error|unhandled exception)\b",
    re.I,
)
SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])-[-A-Za-z0-9_]{12,}\b"),
    re.compile(r"(?i)\b(bearer\s+)[-._~+/A-Za-z0-9=]{12,}"),
    re.compile(r"(?i)\b(api[_ -]?key|access[_ -]?token|secret)\s*[:=]\s*[^\s,;]+"),
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def parse_time(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(item, dict):
                yield item


def tail_jsonl(path: Path, limit: int = 600) -> list[dict[str, Any]]:
    rows: deque[dict[str, Any]] = deque(maxlen=limit)
    for row in iter_jsonl(path):
        rows.append(row)
    return list(rows)


def detect_product(path: Path) -> str:
    text = str(path).lower()
    if ".claude" in text or "claude-worktrees" in text:
        return "claude"
    if ".codex" in text or "rollout-" in path.name.lower():
        return "codex"
    raise ValueError(f"Cannot infer product from path: {path}")


def candidate_paths(product: str) -> Iterable[Path]:
    if product in ("auto", "claude") and CLAUDE_ROOT.exists():
        for path in CLAUDE_ROOT.rglob("*.jsonl"):
            if "subagents" not in {part.lower() for part in path.parts}:
                yield path
    if product in ("auto", "codex") and CODEX_ROOT.exists():
        yield from CODEX_ROOT.rglob("rollout-*.jsonl")


def metadata(path: Path, product: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "product": product,
        "path": str(path),
        "session_id": path.stem if product == "claude" else None,
        "cwd": None,
        "branch": None,
        "updated_at": iso_mtime(path),
        "bytes": path.stat().st_size,
    }
    for index, row in enumerate(iter_jsonl(path)):
        if product == "claude":
            result["session_id"] = row.get("sessionId") or result["session_id"]
            result["cwd"] = row.get("cwd") or result["cwd"]
            result["branch"] = row.get("gitBranch") or result["branch"]
        elif row.get("type") == "session_meta":
            payload = row.get("payload") or {}
            result["session_id"] = payload.get("session_id") or payload.get("id")
            result["cwd"] = payload.get("cwd")
        if index >= 80 and result.get("cwd") and result.get("session_id"):
            break
    return result


def resolve_session(product: str, session: str) -> tuple[Path, str]:
    direct = Path(os.path.expandvars(os.path.expanduser(session)))
    if direct.is_file():
        actual = detect_product(direct) if product == "auto" else product
        return direct.resolve(), actual
    matches = []
    for path in candidate_paths(product):
        if session.lower() in path.name.lower() or session.lower() in str(path).lower():
            matches.append(path)
    if not matches:
        raise FileNotFoundError(f"No session log found for {session!r}")
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    chosen = matches[0]
    actual = detect_product(chosen) if product == "auto" else product
    return chosen, actual


def clip(text: str, limit: int = 500) -> str:
    cleaned = text
    for pattern in SECRET_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def claude_projection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    messages: deque[dict[str, str]] = deque(maxlen=5)
    tools: deque[dict[str, str]] = deque(maxlen=10)
    alerts: deque[dict[str, str]] = deque(maxlen=5)
    last_ts = None
    last_stop = None
    cwd = branch = session_id = None
    for row in rows:
        last_ts = row.get("timestamp") or last_ts
        cwd = row.get("cwd") or cwd
        branch = row.get("gitBranch") or branch
        session_id = row.get("sessionId") or session_id
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        last_stop = message.get("stop_reason") or last_stop
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text" and role == "assistant" and isinstance(part.get("text"), str):
                text = clip(part["text"])
                messages.append({"timestamp": str(last_ts), "text": text})
                if ALERT_RE.search(text):
                    alerts.append({"timestamp": str(last_ts), "text": text})
            elif kind == "tool_use":
                name = str(part.get("name") or "unknown")
                tools.append({"timestamp": str(last_ts), "name": name})
                if name.lower() in {"askuserquestion", "request_user_input", "permissionprompt"}:
                    alerts.append({"timestamp": str(last_ts), "text": f"Tool requested: {name}"})
            elif kind == "tool_result" and part.get("is_error") is True:
                alerts.append({"timestamp": str(last_ts), "text": "A tool returned an error"})
    return {
        "session_id": session_id,
        "cwd": cwd,
        "branch": branch,
        "last_event_at": last_ts,
        "explicit_turn_complete": last_stop == "end_turn",
        "recent_messages": list(messages),
        "recent_tools": list(tools),
        "alerts": list(alerts),
    }


def codex_projection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    messages: deque[dict[str, str]] = deque(maxlen=5)
    tools: deque[dict[str, str]] = deque(maxlen=10)
    alerts: deque[dict[str, str]] = deque(maxlen=5)
    last_ts = None
    complete_index = -1
    activity_index = -1
    session_id = cwd = None
    for index, row in enumerate(rows):
        last_ts = row.get("timestamp") or last_ts
        kind = row.get("type")
        payload = row.get("payload") or {}
        if kind == "session_meta" and isinstance(payload, dict):
            session_id = payload.get("session_id") or payload.get("id") or session_id
            cwd = payload.get("cwd") or cwd
        if kind == "event_msg" and isinstance(payload, dict):
            event_type = payload.get("type")
            if event_type == "agent_message" and isinstance(payload.get("message"), str):
                activity_index = index
                text = clip(payload["message"])
                messages.append({"timestamp": str(last_ts), "text": text})
                if ALERT_RE.search(text):
                    alerts.append({"timestamp": str(last_ts), "text": text})
            elif event_type == "task_complete":
                complete_index = index
                final = payload.get("last_agent_message")
                if isinstance(final, str):
                    messages.append({"timestamp": str(last_ts), "text": clip(final)})
        elif kind == "response_item" and isinstance(payload, dict):
            item_type = payload.get("type")
            if item_type in ("custom_tool_call", "function_call"):
                activity_index = index
                name = str(payload.get("name") or "unknown")
                tools.append({"timestamp": str(last_ts), "name": name})
            elif item_type in ("custom_tool_call_output", "function_call_output"):
                output = payload.get("output")
                if isinstance(output, str) and re.match(r"(?is)^\s*(script failed|error:|fatal:|permission denied)", output):
                    alerts.append({"timestamp": str(last_ts), "text": "A tool output contains an error/blocker indicator"})
    return {
        "session_id": session_id,
        "cwd": cwd,
        "last_event_at": last_ts,
        "explicit_turn_complete": complete_index >= activity_index and complete_index >= 0,
        "recent_messages": list(messages),
        "recent_tools": list(tools),
        "alerts": list(alerts),
    }


def snapshot(path: Path, product: str, stall_seconds: int) -> dict[str, Any]:
    rows = tail_jsonl(path)
    projection = claude_projection(rows) if product == "claude" else codex_projection(rows)
    base = metadata(path, product)
    projection["session_id"] = projection.get("session_id") or base.get("session_id")
    projection["cwd"] = projection.get("cwd") or base.get("cwd")
    if product == "claude":
        projection["branch"] = projection.get("branch") or base.get("branch")
    age = max(0, int(datetime.now(timezone.utc).timestamp() - path.stat().st_mtime))
    last_event_epoch = parse_time(projection.get("last_event_at")) or path.stat().st_mtime
    active_alerts = [
        alert for alert in projection["alerts"]
        if (parse_time(alert.get("timestamp")) or 0) >= last_event_epoch - 5
    ]
    projection["active_alerts"] = active_alerts
    if active_alerts:
        state = "attention"
    elif projection["explicit_turn_complete"]:
        state = "completed_turn"
    elif age >= stall_seconds:
        state = "stalled_candidate"
    else:
        state = "active"
    return {
        "product": product,
        "path": str(path),
        "updated_at": iso_mtime(path),
        "age_seconds": age,
        "state": state,
        "safe_projection": projection,
        "caveat": "Turn completion is not objective completion; verify durable state.",
    }


def command_discover(args: argparse.Namespace) -> int:
    items = []
    for path in candidate_paths(args.product):
        try:
            product = detect_product(path)
            item = metadata(path, product)
        except (OSError, ValueError):
            continue
        if args.cwd and args.cwd.lower() not in str(item.get("cwd") or "").lower() and args.cwd.lower() not in str(path).lower():
            continue
        items.append(item)
    items.sort(key=lambda item: item["updated_at"], reverse=True)
    print(json.dumps(items[: args.limit], indent=2, ensure_ascii=False))
    return 0


def command_snapshot(args: argparse.Namespace) -> int:
    path, product = resolve_session(args.product, args.session)
    print(json.dumps(snapshot(path, product, args.stall_seconds), indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("discover", help="List recent main-session logs")
    discover.add_argument("--product", choices=("auto", "claude", "codex"), default="auto")
    discover.add_argument("--cwd", help="Filter by cwd/path substring")
    discover.add_argument("--limit", type=int, default=10)
    discover.set_defaults(func=command_discover)
    snap = sub.add_parser("snapshot", help="Safely summarize one session")
    snap.add_argument("--product", choices=("auto", "claude", "codex"), default="auto")
    snap.add_argument("--session", required=True, help="Session ID or JSONL path")
    snap.add_argument("--stall-seconds", type=int, default=600)
    snap.set_defaults(func=command_snapshot)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
