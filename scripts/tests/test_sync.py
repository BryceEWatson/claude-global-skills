#!/usr/bin/env python3
"""Hermetic tests for scripts/sync.py — the cross-runtime (Claude/Codex) sync engine.

Every test builds a throwaway repo + throwaway live homes under tmp dirs and points
the engine at them via CLAUDE_GLOBAL_SKILLS_REPO / CLAUDE_SKILLS_DIR / CODEX_SKILLS_DIR,
so nothing ever touches the developer's real ~/.claude or ~/.codex trees.

Run:  python -m unittest discover -s scripts/tests -p 'test_*.py'
"""
import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path

_SYNC_PATH = Path(__file__).resolve().parent.parent / "sync.py"
_spec = importlib.util.spec_from_file_location("sync_under_test", _SYNC_PATH)
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)


def write_skill(repo: Path, name: str, targets=None, shared=None, overlays=None,
                body="# doc\n"):
    """Create <repo>/<name>/ with SKILL.md (+ optional targets), shared files, and
    per-target overlay files. `overlays` = {target: {relpath: content}}."""
    sd = repo / name
    sd.mkdir(parents=True, exist_ok=True)
    fm = "---\nname: %s\n" % name
    if targets is not None:
        fm += "targets: [%s]\n" % ", ".join(targets)
    fm += "---\n"
    (sd / "SKILL.md").write_text(fm + body, encoding="utf-8")
    for rel, content in (shared or {}).items():
        p = sd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for tgt, files in (overlays or {}).items():
        for rel, content in files.items():
            p = sd / sync.OVERLAY_DIR / tgt / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    return sd


class SyncTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.claude = self.tmp / "claude_home"
        self.codex = self.tmp / "codex_home"
        self.repo.mkdir()
        # Homes are created per-test where needed (absence is meaningful).
        self._env = {
            "CLAUDE_GLOBAL_SKILLS_REPO": str(self.repo),
            "CLAUDE_SKILLS_DIR": str(self.claude),
            "CODEX_SKILLS_DIR": str(self.codex),
        }
        self._saved = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def run_mode(self, fn, *a):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fn(*a)
        return code, out.getvalue(), err.getvalue()

    def mk_homes(self, claude=True, codex=True):
        if claude:
            self.claude.mkdir(exist_ok=True)
        if codex:
            self.codex.mkdir(exist_ok=True)


# --------------------------------------------------------------------------- #
# Frontmatter targets parsing
# --------------------------------------------------------------------------- #
class TestTargetsParsing(SyncTestBase):
    def _targets(self, text):
        p = self.repo / "SKILL.md"
        p.write_text(text, encoding="utf-8")
        return sync.read_frontmatter_targets(p)

    def test_absent_defaults_claude(self):
        self.assertEqual(self._targets("# no frontmatter\nbody"), ["claude"])
        self.assertEqual(self._targets("---\nname: x\n---\nbody"), ["claude"])

    def test_flow_list(self):
        self.assertEqual(self._targets("---\ntargets: [claude, codex]\n---\n"),
                         ["claude", "codex"])

    def test_flow_list_with_comment(self):
        self.assertEqual(self._targets("---\ntargets: [claude, codex]  # dual\n---\n"),
                         ["claude", "codex"])

    def test_block_list(self):
        self.assertEqual(self._targets("---\ntargets:\n  - claude\n  - codex\n---\n"),
                         ["claude", "codex"])

    def test_scalar(self):
        self.assertEqual(self._targets("---\ntargets: codex\n---\n"), ["codex"])

    def test_only_leading_frontmatter_parsed(self):
        # A `---` thematic break in the body must not be read as frontmatter.
        self.assertEqual(
            self._targets("---\ntargets: [claude]\n---\nbody\n---\ntargets: [codex]\n"),
            ["claude"])

    def test_unknown_target_raises(self):
        with self.assertRaises(ValueError):
            self._targets("---\ntargets: [claude, gemini]\n---\n")

    def test_malformed_flow_raises(self):
        with self.assertRaises(ValueError):
            self._targets("---\ntargets: [claude\n---\n")

    def test_empty_targets_raises(self):
        with self.assertRaises(ValueError):
            self._targets("---\ntargets: []\n---\n")


# --------------------------------------------------------------------------- #
# Materialization + overlays + substitution
# --------------------------------------------------------------------------- #
class TestMaterialize(SyncTestBase):
    def test_dual_materialize_overlay_and_token(self):
        write_skill(self.repo, "dual", targets=["claude", "codex"],
                    shared={"scripts/run.sh": "echo {{SKILL_HOME}}/x\n"},
                    overlays={"codex": {"agents/openai.yaml": "codex: yes\n"}})
        sd = self.repo / "dual"
        cla = sync.materialize(sd, "claude", "dual")
        cod = sync.materialize(sd, "codex", "dual")
        # Claude: no overlay file, no overlays/ path.
        self.assertNotIn("agents/openai.yaml", cla)
        self.assertFalse(any(k.startswith("overlays/") for k in cla))
        # Codex: overlay file present as agents/openai.yaml (remapped), no overlays/.
        self.assertIn("agents/openai.yaml", cod)
        self.assertFalse(any(k.startswith("overlays/") for k in cod))
        # Token expanded per target.
        self.assertIn(b"$HOME/.claude/skills/dual/x", cla["scripts/run.sh"])
        self.assertIn(b"$HOME/.codex/skills/dual/x", cod["scripts/run.sh"])

    def test_overlay_shadow_is_error(self):
        write_skill(self.repo, "bad", targets=["claude", "codex"],
                    shared={"scripts/run.sh": "shared\n"},
                    overlays={"codex": {"scripts/run.sh": "shadow\n"}})
        with self.assertRaises(ValueError):
            sync.materialize(self.repo / "bad", "codex", "bad")


# --------------------------------------------------------------------------- #
# Deploy
# --------------------------------------------------------------------------- #
class TestDeploy(SyncTestBase):
    def test_claude_only_skill(self):
        self.mk_homes()
        write_skill(self.repo, "c1", targets=["claude"], shared={"a.txt": "x\n"})
        code, out, _ = self.run_mode(sync.do_deploy, None, None)
        self.assertEqual(code, sync.EXIT_OK)
        self.assertTrue((self.claude / "c1" / "a.txt").exists())
        self.assertFalse((self.codex / "c1").exists())

    def test_codex_only_skill(self):
        self.mk_homes()
        write_skill(self.repo, "x1", targets=["codex"], shared={"a.txt": "x\n"})
        self.run_mode(sync.do_deploy, None, None)
        self.assertTrue((self.codex / "x1" / "a.txt").exists())
        self.assertFalse((self.claude / "x1").exists())

    def test_dual_deploy_keeps_codex_files_out_of_claude(self):
        self.mk_homes()
        write_skill(self.repo, "dual", targets=["claude", "codex"],
                    overlays={"codex": {"agents/openai.yaml": "codex: yes\n"}})
        self.run_mode(sync.do_deploy, "both", None)
        # Claude install: NO agents/, NO overlays/
        self.assertTrue((self.claude / "dual" / "SKILL.md").exists())
        self.assertFalse((self.claude / "dual" / "agents").exists())
        self.assertFalse((self.claude / "dual" / "overlays").exists())
        # Codex install: HAS agents/openai.yaml, NO overlays/
        self.assertTrue((self.codex / "dual" / "agents" / "openai.yaml").exists())
        self.assertFalse((self.codex / "dual" / "overlays").exists())

    def test_deploy_explicit_target_creates_home(self):
        self.mk_homes(claude=True, codex=False)  # codex home absent
        write_skill(self.repo, "dual", targets=["claude", "codex"], shared={"a.txt": "x\n"})
        self.assertFalse(self.codex.exists())
        code, out, _ = self.run_mode(sync.do_deploy, "codex", None)
        self.assertEqual(code, sync.EXIT_OK)
        self.assertTrue((self.codex / "dual" / "a.txt").exists())

    def test_default_deploys_only_targets_whose_home_exists(self):
        self.mk_homes(claude=True, codex=False)  # codex home absent
        write_skill(self.repo, "dual", targets=["claude", "codex"], shared={"a.txt": "x\n"})
        code, out, _ = self.run_mode(sync.do_deploy, None, None)
        self.assertEqual(code, sync.EXIT_OK)
        self.assertTrue((self.claude / "dual" / "a.txt").exists())
        self.assertFalse((self.codex / "dual").exists())
        self.assertIn("declares 'codex' but its home is absent", out)

    def test_local_state_preserved_on_redeploy(self):
        self.mk_homes()
        write_skill(self.repo, "c1", targets=["claude"], shared={"a.txt": "one\n"})
        self.run_mode(sync.do_deploy, "claude", None)
        ls = self.claude / "c1" / ".local-state"
        ls.mkdir(parents=True)
        (ls / "state.json").write_text("keep me", encoding="utf-8")
        # change repo content and redeploy
        (self.repo / "c1" / "a.txt").write_text("two\n", encoding="utf-8")
        self.run_mode(sync.do_deploy, "claude", None)
        self.assertEqual((self.claude / "c1" / "a.txt").read_text(), "two\n")
        self.assertTrue((ls / "state.json").exists())
        self.assertEqual((ls / "state.json").read_text(), "keep me")

    def test_unrelated_live_skill_and_sibling_untouched(self):
        self.mk_homes()
        write_skill(self.repo, "c1", targets=["claude"], shared={"a.txt": "x\n"})
        # An unrelated installed skill (has SKILL.md) and a non-skill sibling dir.
        unrel = self.claude / "someone-elses-skill"
        unrel.mkdir(parents=True)
        (unrel / "SKILL.md").write_text("---\nname: someone-elses-skill\n---\n", encoding="utf-8")
        (unrel / "keep.txt").write_text("do not touch", encoding="utf-8")
        sib = self.claude / ".system"
        sib.mkdir()
        (sib / "cfg").write_text("system", encoding="utf-8")
        self.run_mode(sync.do_deploy, "both", None)
        self.assertEqual((unrel / "keep.txt").read_text(), "do not touch")
        self.assertTrue((unrel / "SKILL.md").exists())
        self.assertEqual((sib / "cfg").read_text(), "system")


# --------------------------------------------------------------------------- #
# Check
# --------------------------------------------------------------------------- #
class TestCheck(SyncTestBase):
    def _deploy_dual(self):
        self.mk_homes()
        write_skill(self.repo, "dual", targets=["claude", "codex"],
                    shared={"scripts/run.sh": "echo {{SKILL_HOME}}/x\n"},
                    overlays={"codex": {"agents/openai.yaml": "codex: yes\n"}})
        self.run_mode(sync.do_deploy, "both", None)

    def test_in_sync_after_deploy_both(self):
        self._deploy_dual()
        code, out, _ = self.run_mode(sync.do_check, "both", None)
        self.assertEqual(code, sync.EXIT_OK, out)
        self.assertIn("in sync", out)

    def test_drift_detected_on_live_edit(self):
        self._deploy_dual()
        (self.claude / "dual" / "scripts" / "run.sh").write_text("tampered\n", encoding="utf-8")
        code, out, _ = self.run_mode(sync.do_check, "claude", None)
        self.assertEqual(code, sync.EXIT_DRIFT)
        self.assertIn("run.sh", out)

    def test_check_targets_independently(self):
        self._deploy_dual()
        # tamper codex only
        (self.codex / "dual" / "scripts" / "run.sh").write_text("tampered\n", encoding="utf-8")
        code_c, out_c, _ = self.run_mode(sync.do_check, "claude", None)
        code_x, out_x, _ = self.run_mode(sync.do_check, "codex", None)
        self.assertEqual(code_c, sync.EXIT_OK, out_c)   # claude clean
        self.assertEqual(code_x, sync.EXIT_DRIFT, out_x)  # codex drifted

    def test_localstate_not_reported_as_drift(self):
        self._deploy_dual()
        ls = self.claude / "dual" / ".local-state"
        ls.mkdir(parents=True)
        (ls / "s").write_text("x", encoding="utf-8")
        code, out, _ = self.run_mode(sync.do_check, "claude", None)
        self.assertEqual(code, sync.EXIT_OK, out)

    def test_unmanaged_live_skill_noted_not_drift(self):
        self.mk_homes()
        write_skill(self.repo, "c1", targets=["claude"], shared={"a.txt": "x\n"})
        self.run_mode(sync.do_deploy, "claude", None)
        unrel = self.claude / "unmanaged"
        unrel.mkdir()
        (unrel / "SKILL.md").write_text("---\nname: unmanaged\n---\n", encoding="utf-8")
        code, out, _ = self.run_mode(sync.do_check, "claude", None)
        self.assertEqual(code, sync.EXIT_OK, out)
        self.assertIn("unmanaged live-only", out)

    def test_env_error_when_no_home(self):
        # No homes created at all.
        write_skill(self.repo, "c1", targets=["claude"], shared={"a.txt": "x\n"})
        code, out, err = self.run_mode(sync.do_check, "claude", None)
        self.assertEqual(code, sync.EXIT_ENV)
        self.assertIn("no live skills home", err)


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #
class TestCapture(SyncTestBase):
    def _deploy_dual(self):
        self.mk_homes()
        write_skill(self.repo, "dual", targets=["claude", "codex"],
                    shared={"scripts/run.sh": "echo {{SKILL_HOME}}/x\n"},
                    overlays={"codex": {"agents/openai.yaml": "codex: yes\n"}})
        self.run_mode(sync.do_deploy, "both", None)

    def test_requires_explicit_target(self):
        self.mk_homes()
        for bad in (None, "both"):
            code, out, err = self.run_mode(sync.do_capture, bad, None)
            self.assertEqual(code, sync.EXIT_ERROR)
            self.assertIn("requires an explicit single --target", err)

    def test_roundtrip_edit_preserves_token_and_other_overlay(self):
        self._deploy_dual()
        # Edit the live CLAUDE shared file (token is expanded there).
        live = self.claude / "dual" / "scripts" / "run.sh"
        live.write_text(live.read_text() + "echo hello\n", encoding="utf-8")
        code, out, _ = self.run_mode(sync.do_capture, "claude", None)
        self.assertEqual(code, sync.EXIT_OK, out)
        repo_shared = (self.repo / "dual" / "scripts" / "run.sh").read_text()
        self.assertIn("{{SKILL_HOME}}/x", repo_shared)   # token folded back
        self.assertIn("echo hello", repo_shared)          # new line captured
        # The codex overlay in the repo is untouched.
        self.assertEqual(
            (self.repo / "dual" / "overlays" / "codex" / "agents" / "openai.yaml").read_text(),
            "codex: yes\n")

    def test_refuses_divergence(self):
        self._deploy_dual()
        # Edit BOTH live copies' shared region differently -> true divergence.
        (self.claude / "dual" / "SKILL.md").write_text(
            "---\nname: dual\ntargets: [claude, codex]\n---\nCLAUDE EDIT\n", encoding="utf-8")
        (self.codex / "dual" / "SKILL.md").write_text(
            "---\nname: dual\ntargets: [claude, codex]\n---\nCODEX EDIT\n", encoding="utf-8")
        before = (self.repo / "dual" / "SKILL.md").read_text()
        code, out, _ = self.run_mode(sync.do_capture, "claude", None)
        self.assertEqual(code, sync.EXIT_ERROR)
        self.assertIn("DIVERGENCE", out)
        # Repo unchanged (nothing silently resolved).
        self.assertEqual((self.repo / "dual" / "SKILL.md").read_text(), before)

    def test_stale_secondary_is_not_divergence(self):
        self._deploy_dual()
        # Edit only the claude shared copy; codex stays as-deployed (stale/behind).
        (self.claude / "dual" / "scripts" / "run.sh").write_text(
            "echo $HOME/.claude/skills/dual/x\necho added\n", encoding="utf-8")
        code, out, _ = self.run_mode(sync.do_capture, "claude", None)
        self.assertEqual(code, sync.EXIT_OK, out)
        self.assertIn("echo added", (self.repo / "dual" / "scripts" / "run.sh").read_text())

    def test_refuses_unclassifiable_file(self):
        self._deploy_dual()
        # A new live file that is neither shared nor a claude overlay.
        (self.claude / "dual" / "mystery.txt").write_text("who am i", encoding="utf-8")
        code, out, _ = self.run_mode(sync.do_capture, "claude", None)
        self.assertEqual(code, sync.EXIT_ERROR)
        self.assertIn("unclassifiable", out)
        self.assertFalse((self.repo / "dual" / "mystery.txt").exists())

    def test_capture_new_overlay_file(self):
        self._deploy_dual()
        # Edit the existing codex overlay file live, then capture codex.
        (self.codex / "dual" / "agents" / "openai.yaml").write_text(
            "codex: changed\n", encoding="utf-8")
        code, out, _ = self.run_mode(sync.do_capture, "codex", None)
        self.assertEqual(code, sync.EXIT_OK, out)
        self.assertEqual(
            (self.repo / "dual" / "overlays" / "codex" / "agents" / "openai.yaml").read_text(),
            "codex: changed\n")


# --------------------------------------------------------------------------- #
# Literal-expansion guard
# --------------------------------------------------------------------------- #
class TestLiteralExpansion(SyncTestBase):
    def test_literal_expansion_warns_on_check_and_refuses_capture(self):
        self.mk_homes()
        # Shared file uses a literal install path instead of the token — a smell.
        write_skill(self.repo, "lit", targets=["claude"],
                    shared={"run.sh": "echo $HOME/.claude/skills/lit/x\n"})
        self.run_mode(sync.do_deploy, "claude", None)
        code, out, _ = self.run_mode(sync.do_check, "claude", None)
        self.assertIn("contains literal", out)
        # Capture must refuse because the reverse pass would be ambiguous.
        (self.claude / "lit" / "run.sh").write_text(
            "echo $HOME/.claude/skills/lit/x\necho more\n", encoding="utf-8")
        code, out, _ = self.run_mode(sync.do_capture, "claude", None)
        self.assertEqual(code, sync.EXIT_ERROR)
        self.assertIn("literal SKILL_HOME expansion", out)


if __name__ == "__main__":
    unittest.main()
