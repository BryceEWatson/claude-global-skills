# Changelog

All notable changes to this repository are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This is a deploy-by-copy skills library (each skill is copied to
`~/.claude/skills/<name>/`) rather than a versioned package, so releases are
grouped by **date** instead of strict [Semantic Versioning](https://semver.org/).

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
