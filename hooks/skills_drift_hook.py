#!/usr/bin/env python3
"""PostToolUse drift-detector hook for claude-global-skills.

Fires after Edit / Write / MultiEdit / NotebookEdit. If the edited file lives under
a known live skills home — Claude Code's ~/.claude/skills/ OR Codex's ~/.codex/skills/
— it checks whether that target's live copy has drifted from the repo (via
scripts/sync.py --check --target <t> --skill <name>) and, if so, prints a one-line
stderr nudge with the correctly-targeted capture command.

Model: REPO-AUTHORITATIVE (see scripts/sync.py). For a portable (multi-target) skill
the preferred fix is to edit the repo and redeploy; the nudge says so. The plain
`--capture --target <t>` remains the pull-back path for a one-off live edit.

Homes are env-overridable (matching sync.py): CLAUDE_SKILLS_DIR / CODEX_SKILLS_DIR.

Non-blocking by design: ALWAYS exits 0 and never raises — a sync reminder must never
break a session. Silence it with CLAUDE_SKILLS_DRIFT_HOOK=0. Point it at a non-default
checkout with CLAUDE_GLOBAL_SKILLS_REPO=<path>.

Install: a PostToolUse hook in ~/.claude/settings.json — see README.md.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_REPO = str(Path.home() / "Projects" / "claude-global-skills")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
IN_SYNC_EXIT = 0  # scripts/sync.py --check: 0=in-sync, 3=drift, 2=env-error, other=error


def _home(target: str) -> Path:
    env = os.environ.get("CLAUDE_SKILLS_DIR" if target == "claude" else "CODEX_SKILLS_DIR")
    if env:
        return Path(env).resolve()
    sub = ".claude" if target == "claude" else ".codex"
    return (Path.home() / sub / "skills").resolve()


def _resolve_target_and_skill(edited: Path):
    """Return (target, skill_name) if the edit is under a live skills home, else None."""
    for target in ("claude", "codex"):
        home = _home(target)
        if home in edited.parents:
            try:
                return target, edited.relative_to(home).parts[0]
            except Exception:
                return target, "?"
    return None


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
    except Exception:
        return 0

    resolved = _resolve_target_and_skill(edited)
    if not resolved:
        return 0  # not a global-skill edit — nothing to do
    target, skill = resolved

    repo = Path(os.environ.get("CLAUDE_GLOBAL_SKILLS_REPO", DEFAULT_REPO))
    sync = repo / "scripts" / "sync.py"

    drift = True  # fail-open: if we can't check, nudge rather than miss real drift
    if sync.is_file():
        try:
            r = subprocess.run(
                [sys.executable, str(sync), "--check", "--target", target, "--skill", skill],
                capture_output=True, text=True, timeout=30,
            )
            # Fail-open: only stay silent when this skill/target is PROVABLY in sync
            # (exit 0). Drift (3), env-error (2), or any other code -> nudge.
            drift = (r.returncode != IN_SYNC_EXIT)
        except Exception:
            drift = True

    if drift:
        sys.stderr.write(
            f"[skills-drift] you edited global skill '{skill}' ({target}) — it has "
            f"drifted from claude-global-skills. The repo is canonical: prefer editing "
            f"the repo + `python \"{sync}\" --deploy`. To pull this live edit back: "
            f"python \"{sync}\" --capture --target {target}  (then commit + open a PR).\n"
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
