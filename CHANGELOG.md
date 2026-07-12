# Changelog

All notable changes to this repository are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This is a deploy-by-copy skills library (each skill is copied to
`~/.claude/skills/<name>/`) rather than a versioned package, so releases are
grouped by **date** instead of strict [Semantic Versioning](https://semver.org/).

## [2026-07-11] — Cross-runtime skill sync (Claude Code + Codex)

Skills can now deploy to Claude Code, Codex, or both from one shared source. The
sync engine moves from live-authoritative to **repo-authoritative**.

### Added

- `targets:` frontmatter key — a skill declares `[claude]`, `[codex]`, or
  `[claude, codex]` (absent → `[claude]`, so every existing skill is unchanged).
- Per-target **overlays** (`<skill>/overlays/<target>/`, additive-only) so
  product-only files (e.g. Codex's `agents/openai.yaml`) install to that product and
  never leak into the other. A `{{SKILL_HOME}}` token expands to each target's install
  path, keeping a single shared `SKILL.md` product-correct.
- `scripts/sync.py` gains `--target claude|codex|both`; `--capture` requires an
  explicit target and refuses to silently resolve a divergence between two live
  copies. Homes are env-overridable (`CLAUDE_SKILLS_DIR` / `CODEX_SKILLS_DIR`).
- `monitor-agent-thread` — the first dual-target skill: safely watch a Claude Code or
  Codex session from the other product, with a projection that never exposes hidden
  reasoning, raw tool arguments, signatures, encrypted content, or secrets.
- Test suites for the sync engine, the monitor's privacy invariants (both
  directions), and the drift hook — all wired into CI.

### Changed

- The drift hook is target-aware (detects `.claude/skills` vs `.codex/skills` edits
  and emits the correctly-targeted capture command).
- Docs (`README`, `CONTRIBUTING`, `SKILL-SPEC`) document the canonical model, target
  declaration, overlays, the command surface, and the migration path.

## [2026-06-15] — Open-source preparation

First public-release pass: licensing, contributor docs, privacy/security
documentation, CI, and a portability fix so the skills run outside the
author's machine.

### Added

- `LICENSE` — MIT license for the repository.
- `SECURITY.md` — documents the privacy surface (skills that mine local Claude
  chat history), the fail-closed write guard, and how to report a
  vulnerability.
- `CONTRIBUTING.md` — outside-contributor workflow: edit the repo copy
  directly, set `CLAUDE_GLOBAL_SKILLS_REPO`, and test with
  `scripts/sync.py --deploy`.
- `SKILL-SPEC.md` — the `SKILL.md` contract (YAML frontmatter, `name` must
  equal the directory name) and directory conventions.
- `CODE_OF_CONDUCT.md` — community expectations.
- Continuous integration plus GitHub issue and pull-request templates.

### Changed

- Portability fix (backward-compatible): hardcoded author home paths now
  derive from `Path.home()` / `%USERPROFILE%`, so the skills resolve the
  correct location on any machine.
- Curated the skills into two tiers: **Core** (portable, drop-in) and
  **Personal-example** (wired to the author — adapt before use).

### Security

- `.gitignore` now also covers `.claude/` and `.pytest_cache/`, alongside the
  existing `**/.local-state/`, `.env`, `**/.env`, `__pycache__/`, and `*.pyc`
  entries, to keep local config and mined private data out of the repository.
