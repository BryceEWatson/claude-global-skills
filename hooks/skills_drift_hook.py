#!/usr/bin/env python3
"""PostToolUse drift-detector hook for claude-global-skills.

Fires after Edit / Write / MultiEdit / NotebookEdit. If the edited file lives
under ~/.claude/skills/, it checks whether live skills have drifted from the repo
(via scripts/sync.py --check) and, if so, prints a one-line stderr nudge to
capture the change into the repo.

Model: LIVE-AUTHORITATIVE + CAPTURE (see scripts/sync.py). The nudge points at
`python <repo>/scripts/sync.py --capture`.

Non-blocking by design: ALWAYS exits 0 and never raises — a sync reminder must
never break a session. Silence it with CLAUDE_SKILLS_DRIFT_HOOK=0. Point it at a
non-default checkout with CLAUDE_GLOBAL_SKILLS_REPO=<path>.

Install: a PostToolUse hook in ~/.claude/settings.json — see README.md §"Keeping
the repo in sync".
"""
import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_REPO = str(Path.home() / "Projects" / "claude-global-skills")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
IN_SYNC_EXIT = 0  # scripts/sync.py --check: 0=in-sync, 3=drift, 2=env-error, other=error


def main() -> int:
    if os.environ.get("CLAUDE_SKILLS_DRIFT_HOOK") == "0":
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("tool_name", "") not in EDIT_TOOLS:
        return 0

    ti = payload.get("tool_input") or {}
    fp = ti.get("file_path") or ti.get("notebook_path") or ""
    if not fp:
        return 0
    try:
        edited = Path(fp).resolve()
        skills_dir = (Path.home() / ".claude" / "skills").resolve()
    except Exception:
        return 0
    if skills_dir not in edited.parents:
        return 0  # not a global-skill edit — nothing to do

    try:
        skill = edited.relative_to(skills_dir).parts[0]
    except Exception:
        skill = "?"

    repo = Path(os.environ.get("CLAUDE_GLOBAL_SKILLS_REPO", DEFAULT_REPO))
    sync = repo / "scripts" / "sync.py"

    drift = True  # fail-open: if we can't check, nudge rather than miss real drift
    if sync.is_file():
        try:
            r = subprocess.run(
                [sys.executable, str(sync), "--check", "--skill", skill],
                capture_output=True, text=True, timeout=30,
            )
            # Fail-open: only stay silent when this skill is PROVABLY in sync
            # (exit 0). Drift (3), env-error (2), or any other code -> nudge,
            # so a misconfigured environment can't masquerade as "in sync".
            drift = (r.returncode != IN_SYNC_EXIT)
        except Exception:
            drift = True

    if drift:
        sys.stderr.write(
            f"[skills-drift] you edited global skill '{skill}' — it has drifted "
            f"from claude-global-skills. Capture it: "
            f"python \"{sync}\" --capture  (then commit + open a PR).\n"
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
