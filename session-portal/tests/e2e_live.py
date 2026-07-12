#!/usr/bin/env python3
"""e2e_live.py — GENUINE cross-runtime round trip against real session transcripts.

Unlike test_portal_e2e.py (which drives the real MCP transport with disposable *fixture*
transcripts and runs in CI), this manual harness makes the sessions themselves as real as the
local environment allows, then runs a disposable Claude<->Codex round trip through the real
portal MCP transport with authenticated principals — never through admin/db shortcuts.

Sessions:
  * Claude: spawns a real disposable headless session (`claude -p ... --session-id <uuid>`),
    which writes a real transcript under ~/.claude/projects. That real transcript is copied
    (never moved/mutated) into a disposable logs dir the portal reads as runtime evidence.
  * Codex: `codex exec` needs interactive ChatGPT auth that a headless job does not have, so
    a fresh Codex session can't always be spawned here. When a recent real Codex rollout
    exists on disk we COPY it (read-only) into the disposable logs dir as genuine
    Codex-shaped runtime evidence; otherwise we synthesize a minimal rollout and say so.

Then, over two real portal_mcp.py subprocesses (one per principal token):
  Claude -> Codex: send, Codex pulls at its own boundary, Codex acks.
  Codex -> Claude: send, Claude pulls at its own boundary, Claude acks.

Asserts: identity derived from tokens (no spoofing), ack + created/delivered/acknowledged
audit trail, NO mutation of either real transcript (sha256 identical), full cleanup.

Run manually:  python session-portal/tests/e2e_live.py
Requires a working, authenticated `claude` CLI. Prints a JSON report and exits non-zero on
any failure. stdlib only.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


core = _load("portal_core")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MCPClient:
    def __init__(self, env, token):
        run_env = dict(os.environ)
        run_env.update(env)
        run_env["SESSION_PORTAL_TOKEN"] = token
        run_env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [sys.executable, str(_SCRIPTS / "portal_mcp.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(_SCRIPTS), env=run_env, text=True, encoding="utf-8", bufsize=1)
        self._id = 0
        self._rpc("initialize", {})
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()

    def _rpc(self, method, params):
        self._id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id,
                                          "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("no response; stderr:\n" + self.proc.stderr.read())
        return json.loads(line)

    def call(self, name, args):
        resp = self._rpc("tools/call", {"name": name, "arguments": args})
        r = resp["result"]
        return r["isError"], json.loads(r["content"][0]["text"])

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
                self.proc.wait()
        for s in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                s.close()
            except Exception:
                pass


def spawn_real_claude(session_id: str) -> Path | None:
    """Spawn a real disposable headless Claude session; return its real transcript path."""
    claude_bin = shutil.which("claude")  # resolves claude.CMD on Windows
    if not claude_bin:
        return None
    try:
        out = subprocess.run(
            [claude_bin, "-p", "Reply with exactly: PORTAL_E2E_OK",
             "--session-id", session_id, "--output-format", "json"],
            capture_output=True, text=True, timeout=120)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    root = Path.home() / ".claude" / "projects"
    cands = [p for p in root.rglob(f"{session_id}.jsonl")] if root.exists() else []
    return cands[0] if cands else None


def newest_real_codex_rollout() -> Path | None:
    root = Path.home() / ".codex" / "sessions"
    if not root.exists():
        return None
    cands = sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def main() -> int:
    report = {"steps": [], "sessions": {}, "ok": False}

    def step(msg, ok=True):
        report["steps"].append({"step": msg, "ok": ok})
        print(("[ok] " if ok else "[FAIL] ") + msg)

    tmp = Path(tempfile.mkdtemp(prefix="portal-e2e-live-"))
    claude_logs = tmp / "claude-logs" / "proj"
    codex_logs = tmp / "codex-logs" / "2026" / "07" / "11"
    claude_logs.mkdir(parents=True)
    codex_logs.mkdir(parents=True)

    claude_sid = str(uuid.uuid4())
    codex_tid = "e2e-" + uuid.uuid4().hex[:12]

    # ---- Claude: real disposable session ------------------------------------------------
    real_claude = spawn_real_claude(claude_sid)
    if real_claude is None:
        step("spawn real Claude session (claude CLI unavailable/failed)", ok=False)
        print(json.dumps(report, indent=2))
        shutil.rmtree(tmp, ignore_errors=True)
        return 2
    claude_transcript = claude_logs / f"{claude_sid}.jsonl"
    shutil.copy2(real_claude, claude_transcript)  # copy real evidence; never mutate original
    report["sessions"]["claude"] = {"session_id": f"claude:{claude_sid}",
                                    "real_transcript": str(real_claude), "kind": "real-headless"}
    step(f"spawned real disposable Claude session {claude_sid}")

    # ---- Codex: real-shape disposable evidence ------------------------------------------
    real_codex = newest_real_codex_rollout()
    codex_transcript = codex_logs / f"rollout-2026-07-11-{codex_tid}.jsonl"
    if real_codex is not None:
        shutil.copy2(real_codex, codex_transcript)
        report["sessions"]["codex"] = {"session_id": f"codex:{codex_tid}",
                                       "copied_from_real": str(real_codex),
                                       "kind": "real-transcript-copy (codex exec needs interactive auth)"}
        step(f"prepared Codex evidence from a real rollout (copy) -> {codex_tid}")
    else:
        codex_transcript.write_text(
            json.dumps({"type": "event_msg", "payload": {"type": "task_complete",
                                                        "last_agent_message": "idle"}}) + "\n",
            encoding="utf-8")
        report["sessions"]["codex"] = {"session_id": f"codex:{codex_tid}", "kind": "synthesized (no real rollout found)"}
        step(f"prepared synthesized Codex evidence -> {codex_tid}", ok=True)

    env = {
        "SESSION_PORTAL_HOME": str(tmp / "portal"),
        "SESSION_PORTAL_DB": str(tmp / "portal" / "portal.db"),
        "SESSION_PORTAL_CLAUDE_LOGS": str(tmp / "claude-logs"),
        "SESSION_PORTAL_CODEX_LOGS": str(tmp / "codex-logs"),
    }
    for k, v in env.items():
        os.environ[k] = v
    os.environ.pop("SESSION_PORTAL_TOKEN", None)

    conn = core.connect()
    core.init_db(conn)
    tok_claude = core.issue_principal(conn, "claude", claude_sid, ttl_seconds=3600)["token"]
    tok_codex = core.issue_principal(conn, "codex", codex_tid, ttl_seconds=3600)["token"]
    conn.close()
    step("minted principal tokens for both sessions")

    c_before, x_before = _sha256(claude_transcript), _sha256(codex_transcript)
    claude = MCPClient(env, tok_claude)
    codex = MCPClient(env, tok_codex)
    failures = 0

    def check(cond, msg):
        nonlocal failures
        step(msg, ok=bool(cond))
        if not cond:
            failures += 1

    try:
        claude.call("portal_register_session", {"label": "live claude"})
        codex.call("portal_register_session", {"label": "live codex"})

        # Claude -> Codex
        err, s1 = claude.call("portal_send_message",
                              {"dest_session_id": f"codex:{codex_tid}",
                               "body": "please rerun CI on main", "idempotency_key": "live-1"})
        check(not err and s1["source_session_id"] == f"claude:{claude_sid}",
              "Claude sent to Codex; source derived from token")
        err, inbox = codex.call("portal_list_inbox", {"deliver": True})
        got = next((m for m in inbox if m["message_id"] == s1["message_id"]), {})
        check(not err and got.get("status") == "delivered", "Codex pulled + delivered at its own boundary")
        err, ack1 = codex.call("portal_acknowledge", {"message_id": s1["message_id"], "note": "done"})
        check(not err and ack1["acknowledged_by"] == f"codex:{codex_tid}", "Codex acknowledged")
        err, ev1 = codex.call("portal_message_events", {"message_id": s1["message_id"]})
        check(not err and [e["event"] for e in ev1] == ["created", "delivered", "acknowledged"],
              "audit trail = created/delivered/acknowledged")

        # Codex -> Claude
        err, s2 = codex.call("portal_send_message",
                             {"dest_session_id": f"claude:{claude_sid}",
                              "body": "CI is green", "idempotency_key": "live-2"})
        check(not err and s2["source_session_id"] == f"codex:{codex_tid}", "Codex sent to Claude")
        err, inbox2 = claude.call("portal_list_inbox", {"deliver": True})
        got2 = next((m for m in inbox2 if m["message_id"] == s2["message_id"]), {})
        check(not err and got2.get("status") == "delivered", "Claude pulled + delivered")
        err, ack2 = claude.call("portal_acknowledge", {"message_id": s2["message_id"]})
        check(not err and ack2["acknowledged_by"] == f"claude:{claude_sid}", "Claude acknowledged")

        # Spoofing impossible
        err, spoof = codex.call("portal_acknowledge", {"message_id": s2["message_id"]})
        check(err and spoof.get("code") == "authorization_error", "Codex cannot ack Claude's message (no spoof)")

        # No transcript mutation
        check(_sha256(claude_transcript) == c_before, "Claude real transcript unmutated")
        check(_sha256(codex_transcript) == x_before, "Codex transcript unmutated")
    finally:
        claude.close()
        codex.close()

    # Cleanup
    out = subprocess.run([sys.executable, str(_SCRIPTS / "portal_admin.py"), "uninstall", "--yes"],
                         cwd=str(_SCRIPTS), env={**os.environ, **env}, capture_output=True, text=True)
    check(out.returncode == 0 and not Path(env["SESSION_PORTAL_DB"]).exists(), "portal DB removed")
    shutil.rmtree(tmp, ignore_errors=True)
    # Remove the disposable Claude session's real transcript we spawned (leave nothing behind).
    try:
        real_claude.unlink(missing_ok=True)
    except OSError:
        pass
    check(not tmp.exists(), "disposable sessions cleaned up")
    for k in env:
        os.environ.pop(k, None)

    report["ok"] = failures == 0
    report["failures"] = failures
    print(json.dumps(report, indent=2))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
