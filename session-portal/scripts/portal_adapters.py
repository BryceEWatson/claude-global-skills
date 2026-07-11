#!/usr/bin/env python3
"""portal_adapters.py — boundary-safe delivery adapters.

These sit between the durable queue (portal_core) and the two runtimes. They decide
WHEN it is safe to hand a queued message over, and they always err toward leaving it
queued. None of them writes a transcript or resumes a live session.

  push_deliver          Deliver only if the recipient is provably paused (idle /
                        waiting-for-user). An active session is REFUSED; the message
                        stays queued.
  claude_resume_plan    The ONLY path that could touch a closed Claude session. It
                        refuses unless the session is proven closed (completed) AND a
                        destination lease is held. It never resumes an active session,
                        and by default is a dry-run that returns a plan rather than
                        spawning `claude --resume`.
  codex_native_status   A local MCP server cannot assume access to host-only Codex task
                        tools, so native task messaging is reported unavailable by
                        default; delivery to Codex falls back to the durable queue, which
                        Codex drains at a supported boundary (session-start / prompt-submit
                        / stop / command / heartbeat).

stdlib only.
"""
from __future__ import annotations

import os
from typing import Any

import portal_core as core
import portal_state as pstate


def push_deliver(conn, message_id: str, product: str, runtime_session: str, *,
                 process_alive: bool | None = None, holder: str | None = None,
                 now: float | None = None) -> dict[str, Any]:
    """Attempt a sender-side push. Classifies the recipient conservatively and refuses
    unless it is push-deliverable (idle / waiting-for-user). Never delivers to an active
    or unproven session."""
    cls = pstate.classify(product, runtime_session, process_alive=process_alive, now=now)
    holder = holder or f"push:{message_id}"
    if not pstate.is_push_deliverable(cls["state"]):
        core._log_event(conn, message_id,
                        "delivery_refused", f"push refused: dest state={cls['state']} ({cls['reason']})")
        out = core.get_message_status(conn, message_id)
        out.update({"delivered": False, "dest_state": cls["state"],
                    "refused_reason": cls["reason"]})
        return out
    res = core.deliver_one(conn, message_id, mode="push", boundary=None,
                           dest_state=cls["state"], holder=holder)
    res["dest_state"] = cls["state"]
    return res


def claude_resume_plan(conn, message_id: str, runtime_session: str, *,
                       process_alive: bool | None = None, holder: str | None = None,
                       execute: bool = False, now: float | None = None) -> dict[str, Any]:
    """Guarded Claude `--resume` adapter. Delivers to a *closed* Claude session only.

    Hard refusals (return without delivering, nothing spawned):
      * session state is not `completed` (i.e. not proven closed) -> refuse. In
        particular an `active` session is never resumed.
      * a destination lease cannot be acquired -> refuse.
    Even when both hold, `execute` defaults to False: we return the command plan rather
    than launching a process, so resuming stays an explicit, operator-driven action.
    """
    cls = pstate.classify("claude", runtime_session, process_alive=process_alive, now=now)
    holder = holder or f"resume:{message_id}"
    if cls["state"] != pstate.COMPLETED:
        core._log_event(conn, message_id, "delivery_refused",
                        f"resume refused: session not proven closed (state={cls['state']})")
        out = core.get_message_status(conn, message_id)
        out.update({"delivered": False, "resumed": False, "dest_state": cls["state"],
                    "refused_reason": f"not closed ({cls['state']})"})
        return out
    if not core.acquire_lease(conn, f"claude:{runtime_session}", holder):
        out = core.get_message_status(conn, message_id)
        out.update({"delivered": False, "resumed": False, "refused_reason": "lease_held"})
        return out
    try:
        plan = ["claude", "--resume", runtime_session]
        if not execute:
            core._log_event(conn, message_id, "resume_planned",
                            "dry-run: closed session + lease held; not spawned")
            out = core.get_message_status(conn, message_id)
            out.update({"delivered": False, "resumed": False, "dry_run": True,
                        "resume_command": plan, "dest_state": cls["state"]})
            return out
        # A real spawn would go here, behind an explicit operator opt-in. We deliberately
        # do not launch a process from this library; the message is marked delivered only
        # once the resumed session pulls it. So even with execute=True we record intent
        # and keep the message queued for the resumed session to drain.
        core._log_event(conn, message_id, "resume_requested", f"cmd={' '.join(plan)}")
        out = core.get_message_status(conn, message_id)
        out.update({"delivered": False, "resumed": True, "resume_command": plan,
                    "dest_state": cls["state"]})
        return out
    finally:
        core.release_lease(conn, f"claude:{runtime_session}", holder)


def codex_native_status() -> dict[str, Any]:
    """Whether Codex native task messaging is usable from here. Default: unavailable.
    Override only in a context that genuinely surfaces the task tool to a running,
    authorized Codex task (set SESSION_PORTAL_CODEX_NATIVE=1)."""
    available = os.environ.get("SESSION_PORTAL_CODEX_NATIVE") == "1"
    return {
        "native_available": available,
        "fallback": "durable_queue",
        "note": ("native task messaging surfaced to the running task"
                 if available else
                 "no host-only Codex task tool; leaving delivery to the durable queue"),
    }


def codex_deliver(conn, message_id: str, runtime_session: str, *,
                  boundary: str | None = None, now: float | None = None) -> dict[str, Any]:
    """Deliver to Codex. If native messaging is unavailable (the default), the message is
    left queued for Codex to drain at a supported boundary; only an explicit safe
    boundary (a Codex-side pull) actually transitions it to delivered."""
    status = codex_native_status()
    if not status["native_available"]:
        if boundary in core.SAFE_PULL_BOUNDARIES:
            res = core.deliver_one(conn, message_id, mode="pull", boundary=boundary,
                                   dest_state=None, holder=f"codex-pull:{runtime_session}")
            res["via"] = "queue_boundary_pull"
            return res
        core._log_event(conn, message_id, "delivery_refused",
                        "codex native unavailable; no safe boundary -> left queued")
        out = core.get_message_status(conn, message_id)
        out.update({"delivered": False, "via": "queue", "refused_reason": "no_native_no_boundary"})
        return out
    # Native path (only when genuinely surfaced): treat as an authorized boundary pull.
    res = core.deliver_one(conn, message_id, mode="pull", boundary="command",
                           dest_state=None, holder=f"codex-native:{runtime_session}")
    res["via"] = "native_task_message"
    return res
