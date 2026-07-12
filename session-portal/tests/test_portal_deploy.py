#!/usr/bin/env python3
"""Guards that the REAL session-portal skill materializes correctly through the PR #15
sync engine: Codex-only files stay out of the Claude install, the {{SKILL_HOME}} token
expands per product, and no shared file carries a literal expansion (which would break
capture). This runs against the repo copy, so it also catches an accidental overlay shadow.
"""
import importlib.util
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SYNC = _REPO / "scripts" / "sync.py"


def _load_sync():
    spec = importlib.util.spec_from_file_location("sync_for_portal", _SYNC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sync = _load_sync()
SKILL_DIR = _REPO / "session-portal"


class TestPortalMaterialization(unittest.TestCase):
    def test_declares_both_targets(self):
        targets = sync.read_frontmatter_targets(SKILL_DIR / "SKILL.md")
        self.assertEqual(sorted(targets), ["claude", "codex"])

    def test_codex_overlay_only_reaches_codex(self):
        cla = sync.materialize(SKILL_DIR, "claude", "session-portal")
        cod = sync.materialize(SKILL_DIR, "codex", "session-portal")
        self.assertIn("agents/openai.yaml", cod)
        self.assertNotIn("agents/openai.yaml", cla)
        # No overlays/ path leaks into either materialized install.
        self.assertFalse(any(k.startswith("overlays/") for k in cla))
        self.assertFalse(any(k.startswith("overlays/") for k in cod))

    def test_skill_home_token_expands_per_product(self):
        cla = sync.materialize(SKILL_DIR, "claude", "session-portal")
        cod = sync.materialize(SKILL_DIR, "codex", "session-portal")
        # Derive the expected expansions from the engine so this test file itself never
        # contains a literal expansion (the sync scanner would flag that as a smell).
        exp_claude = sync.skill_home_expansion("claude", "session-portal").encode()
        exp_codex = sync.skill_home_expansion("codex", "session-portal").encode()
        self.assertIn(exp_claude, cla["SKILL.md"])
        self.assertIn(exp_codex, cod["SKILL.md"])
        # And the raw token must be gone after materialization.
        self.assertNotIn(sync.SKILL_HOME_TOKEN.encode(), cla["SKILL.md"])

    def test_no_literal_expansion_in_shared_files(self):
        self.assertEqual(sync.check_no_literal_expansion(SKILL_DIR, "session-portal"), [])

    def test_core_scripts_present_in_both(self):
        for target in ("claude", "codex"):
            mat = sync.materialize(SKILL_DIR, target, "session-portal")
            for rel in ("scripts/portal_core.py", "scripts/portal_mcp.py",
                        "scripts/portal_state.py", "scripts/portal_adapters.py",
                        "scripts/portal_admin.py"):
                self.assertIn(rel, mat, f"{rel} missing from {target} install")


if __name__ == "__main__":
    unittest.main()
