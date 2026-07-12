#!/usr/bin/env python3
"""portal_adapters.py — advisory classification for the PULL-ONLY portal.

The portal delivers on a single honest event: the AUTHENTICATED recipient pulls its own
inbox (portal_core.list_inbox / deliver_one). That pull is the only proof of runtime
acceptance, so it is the only thing that ever marks a message `delivered`.

Everything in this module is therefore ADVISORY. None of it transitions a message, none of
it writes a transcript, none of it resumes or spawns a session, and none of it fabricates a
"delivered", "resumed", or "native-delivered" receipt the portal cannot back with a real
pull. These helpers answer read-only questions ("is the recipient at a safe point?", "what
would a resume command look like?") so a caller can decide whether to notify a human or wait.

Why no push / auto-resume / native-task delivery in this MVP: each of those would have to
CLAIM the recipient received the message without the recipient ever confirming it. That is
exactly the false-receipt failure the audit flagged. Until there is a real runtime
acceptance channel, delivery stays pull-only and these adapters stay advisory.

stdlib only.
"""
from __future__ import annotations

import os
from typing import Any

import portal_core as core
import portal_state as pstate


def classify_deliverability(product: str, runtime_session: str, *,
                            now: float | None = None) -> dict[str, Any]:
    """Read-only advisory: classify the recipient from its own runtime-owned log (and
    host liveness evidence) and report whether a pull by that recipient would be a safe
    moment. Never delivers, never writes anything. `would_pull_safely` is True when the
    recipient is provably between turns (idle / waiting-for-user); an `active`, `unknown`,
    `stale`, or `unavailable` recipient is reported as not-yet-safe so a caller waits."""
    cls = pstate.classify(product, runtime_session, now=now)
    return {
        "product": product,
        "runtime_session": runtime_session,
        "state": cls["state"],
        "reason": cls["reason"],
        "age_seconds": cls.get("age_seconds"),
        "would_pull_safely": pstate.is_push_deliverable(cls["state"]),
        "advisory_only": True,
        "note": "the portal delivers only when the authenticated recipient pulls; this is advice, not delivery",
    }


def resume_plan(runtime_session: str, *, now: float | None = None) -> dict[str, Any]:
    """Return an OPERATOR resume plan for a Claude session — a description only. This never
    spawns `claude --resume`, never marks a message delivered or resumed, and is not on the
    delivery path. It refuses to even suggest resuming unless the session is proven closed
    (explicit turn-complete + host evidence the process is gone); an active or merely-idle
    session yields no resume suggestion. Resuming is an explicit, human-driven action that
    lives outside this pull-only MVP."""
    cls = pstate.classify("claude", runtime_session, now=now)
    if cls["state"] != pstate.COMPLETED:
        return {
            "resumable": False,
            "state": cls["state"],
            "reason": f"not proven closed (state={cls['state']}); no resume suggested",
            "advisory_only": True,
        }
    return {
        "resumable": True,
        "state": cls["state"],
        "suggested_command": ["claude", "--resume", runtime_session],
        "advisory_only": True,
        "note": ("operator action only: running this is a human decision; the message stays "
                 "queued and is delivered only when the resumed session pulls it"),
    }


def codex_native_status() -> dict[str, Any]:
    """Codex native task messaging is NOT implemented as a delivery channel in this MVP.

    A local MCP server cannot prove a message was accepted by a running Codex task, so we do
    not claim a native delivery. Delivery to Codex is via the durable queue, drained by an
    authenticated Codex-side pull at a real boundary. The env var that previously forced a
    synthetic "native delivered" receipt has been removed; if it is set we still report
    native as unavailable rather than fabricate acceptance."""
    forced = os.environ.get("SESSION_PORTAL_CODEX_NATIVE") == "1"
    return {
        "native_available": False,
        "delivery_channel": "durable_queue_pull",
        "note": ("native task messaging is not an implemented delivery channel; delivery to "
                 "Codex happens when the Codex session pulls its inbox"),
        "ignored_forcing_env": forced,
    }
