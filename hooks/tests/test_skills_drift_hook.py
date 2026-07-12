#!/usr/bin/env python3
"""Tests for hooks/skills_drift_hook.py — target resolution + nudge formatting.

The subprocess call to sync.py --check is stubbed so these tests exercise the HOOK's
own logic (which target/skill an edit maps to, how a drift exit code becomes a nudge)
without re-testing the sync engine.

Run:  python -m unittest discover -s hooks/tests -p 'test_*.py'
"""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import types
import unittest
from pathlib import Path

_HOOK_PATH = Path(__file__).resolve().parent.parent / "skills_drift_hook.py"
_spec = importlib.util.spec_from_file_location("drift_hook_under_test", _HOOK_PATH)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

_REPO = Path(__file__).resolve().parent.parent.parent  # has scripts/sync.py


class HookBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.claude = self.tmp / "claude_home"
        self.codex = self.tmp / "codex_home"
        self.claude.mkdir()
        self.codex.mkdir()
        self._env = {
            "CLAUDE_SKILLS_DIR": str(self.claude),
            "CODEX_SKILLS_DIR": str(self.codex),
            "CLAUDE_GLOBAL_SKILLS_REPO": str(_REPO),
        }
        self._saved = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)
        self._orig_run = hook.subprocess.run

    def tearDown(self):
        hook.subprocess.run = self._orig_run
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def stub_run(self, returncode):
        def _run(*a, **k):
            return types.SimpleNamespace(returncode=returncode, stdout="", stderr="")
        hook.subprocess.run = _run

    def run_hook(self, payload):
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(json.dumps(payload))
        old_stdin = hook.sys.stdin
        hook.sys.stdin = stdin
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = hook.main()
        finally:
            hook.sys.stdin = old_stdin
        return code, err.getvalue()


class TestResolution(HookBase):
    def test_resolves_claude(self):
        edited = self.claude / "myskill" / "SKILL.md"
        self.assertEqual(hook._resolve_target_and_skill(edited), ("claude", "myskill"))

    def test_resolves_codex(self):
        edited = self.codex / "myskill" / "scripts" / "x.py"
        self.assertEqual(hook._resolve_target_and_skill(edited), ("codex", "myskill"))

    def test_unrelated_path_is_none(self):
        self.assertIsNone(hook._resolve_target_and_skill(self.tmp / "elsewhere" / "f.py"))


class TestNudge(HookBase):
    def _payload(self, path):
        return {"tool_name": "Edit", "tool_input": {"file_path": str(path)}}

    def test_drift_nudges_with_correct_target_claude(self):
        self.stub_run(3)  # drift
        code, err = self.run_hook(self._payload(self.claude / "s" / "SKILL.md"))
        self.assertEqual(code, 0)
        self.assertIn("[skills-drift]", err)
        self.assertIn("--capture --target claude", err)

    def test_drift_nudges_with_correct_target_codex(self):
        self.stub_run(3)
        code, err = self.run_hook(self._payload(self.codex / "s" / "SKILL.md"))
        self.assertIn("--capture --target codex", err)

    def test_in_sync_is_silent(self):
        self.stub_run(0)  # in sync
        code, err = self.run_hook(self._payload(self.claude / "s" / "SKILL.md"))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_non_edit_tool_ignored(self):
        self.stub_run(3)
        code, err = self.run_hook({"tool_name": "Bash", "tool_input": {"file_path": str(self.claude / "s" / "x")}})
        self.assertEqual(err, "")

    def test_edit_outside_homes_ignored(self):
        self.stub_run(3)
        code, err = self.run_hook(self._payload(self.tmp / "other" / "f.py"))
        self.assertEqual(err, "")

    def test_disabled_by_env(self):
        os.environ["CLAUDE_SKILLS_DRIFT_HOOK"] = "0"
        try:
            self.stub_run(3)
            code, err = self.run_hook(self._payload(self.claude / "s" / "SKILL.md"))
            self.assertEqual(err, "")
        finally:
            os.environ.pop("CLAUDE_SKILLS_DRIFT_HOOK", None)


if __name__ == "__main__":
    unittest.main()
