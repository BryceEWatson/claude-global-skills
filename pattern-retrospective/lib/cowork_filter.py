#!/usr/bin/env python3
"""cowork_filter.py — corpus enumeration + line-shape filter for pattern-retrospective.

Mirrors the ``chat-history-search`` SKILL.md §3 line-shape filter rules so retros do not
re-invent it (the May 2026 ShopForge retro wrote six bespoke extractors before this helper
existed). Programmatic use only — emits one JSONL record per kept user prompt on stdout.

This is what pattern-retrospective's SKILL.md §14 references for "corpus enumeration +
line-shape filtering." It iterates both corpora:

  - Cowork  : %APPDATA%/Claude/local-agent-mode-sessions (Windows)
              ~/Library/Application Support/Claude/local-agent-mode-sessions (macOS)
              (Linux: Cowork corpus not available)
  - CLI     : %USERPROFILE%/.claude/projects (Windows) or ~/.claude/projects (macOS/Linux)

KEEP three line shapes (typed user prompts: CLI array-content, Cowork audit string-content,
queue-operation enqueue events) and DROP the rest (tool_result wrappers, task-notification
wrappers, isSidechain, isApiErrorMessage, attachments, system events).

Used programmatically by retro scripts and as a CLI for ad hoc enumeration.
"""
import argparse
import json
import os
import sys
from pathlib import Path

# Corpus roots. Per-platform:
#   Windows: %APPDATA% / %USERPROFILE% conventions
#   macOS:   ~/Library/Application Support/Claude + ~/.claude
#   Linux:   ~/.claude only (Cowork is Windows + macOS only)
# Falls back to Path.home() so this also works under WSL / when env vars are absent.
def _resolve_cowork_root():
    if sys.platform == 'darwin':
        candidate = Path.home() / 'Library' / 'Application Support' / 'Claude' / 'local-agent-mode-sessions'
        if not candidate.exists():
            print(f'[cowork_filter] WARNING: Cowork root not found at {candidate}', file=sys.stderr)
        return candidate
    if sys.platform.startswith('linux'):
        # Cowork is not distributed for Linux; return a non-existent sentinel path
        # so callers see .exists() == False and can decide what to do.
        return Path.home() / '.cowork-not-available-on-linux'
    # Windows (and anything else): use %APPDATA% convention.
    appdata = os.environ.get('APPDATA')
    if appdata:
        return Path(appdata) / 'Claude' / 'local-agent-mode-sessions'
    home = Path.home()
    candidate = home / 'AppData' / 'Roaming' / 'Claude' / 'local-agent-mode-sessions'
    if not candidate.exists():
        print(f'[cowork_filter] WARNING: Cowork root not found at {candidate}', file=sys.stderr)
    return candidate

def _resolve_cli_root():
    if sys.platform == 'darwin' or sys.platform.startswith('linux'):
        candidate = Path.home() / '.claude' / 'projects'
        if not candidate.exists():
            print(f'[cowork_filter] WARNING: CLI root not found at {candidate}', file=sys.stderr)
        return candidate
    # Windows (and anything else): use %USERPROFILE% convention.
    userprofile = os.environ.get('USERPROFILE')
    if userprofile:
        return Path(userprofile) / '.claude' / 'projects'
    home = Path.home()
    candidate = home / '.claude' / 'projects'
    if not candidate.exists():
        print(f'[cowork_filter] WARNING: CLI root not found at {candidate}', file=sys.stderr)
    return candidate

COWORK_ROOT = _resolve_cowork_root()
CLI_ROOT    = _resolve_cli_root()

DROP_PREFIXES = ('<task-notification>', '<scheduled-task', '<system-reminder>')

def is_typed_user_prompt(rec):
    """Mirrors chat-history-search SKILL.md §3 line-shape filter."""
    if rec.get('type') == 'queue-operation' and rec.get('operation') == 'enqueue':
        text = rec.get('content')
        if isinstance(text, str):
            text = text.strip()
            if text and not text.startswith(DROP_PREFIXES):
                return text
        return None
    if rec.get('type') != 'user': return None
    if rec.get('isSidechain') or rec.get('isApiErrorMessage'): return None
    msg = rec.get('message') or {}
    if msg.get('role') != 'user': return None
    content = msg.get('content')
    text = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for item in content:
            if not isinstance(item, dict): continue
            if item.get('type') == 'tool_result': return None
            if item.get('type') == 'text': text = item.get('text'); break
    if not text: return None
    text = text.strip()
    if not text or text.startswith(DROP_PREFIXES): return None
    return text

def iter_prompts(corpus_root, project_filter=None):
    for jsonl in corpus_root.rglob('*.jsonl'):
        if project_filter and project_filter not in str(jsonl): continue
        with jsonl.open(encoding='utf-8', errors='replace') as f:
            for line in f:
                try: rec = json.loads(line)
                except json.JSONDecodeError: continue
                text = is_typed_user_prompt(rec)
                if text: yield {'path': str(jsonl), 'ts': rec.get('timestamp'), 'text': text}

def _main():
    ap = argparse.ArgumentParser(
        description='Enumerate typed user prompts from Cowork + CLI corpora.',
        epilog='Mirrors chat-history-search SKILL.md §3 line-shape filter. '
               'See ~/.claude/skills/pattern-retrospective/SKILL.md §14.'
    )
    ap.add_argument('--corpus', choices=('cowork', 'cli', 'both'), default='both',
                    help='Which corpus to scan (default: both).')
    ap.add_argument('--project', default=None,
                    help='Substring filter applied to each .jsonl path (e.g. project slug).')
    ap.add_argument('--output-format', choices=('jsonl', 'text'), default='jsonl',
                    help='jsonl: one JSON record per line (default). text: tab-separated path/ts/text.')
    args = ap.parse_args()

    roots = []
    if args.corpus in ('cowork', 'both'):
        if sys.platform.startswith('linux') and args.corpus == 'cowork':
            print('[cowork_filter] Cowork corpus not available on Linux; '
                  'use --corpus cli or run on Windows/macOS.', file=sys.stderr)
        roots.append(COWORK_ROOT)
    if args.corpus in ('cli', 'both'):    roots.append(CLI_ROOT)

    if not any(r.exists() for r in roots):
        print('[cowork_filter] ERROR: no corpus root resolved/exists; nothing to scan.',
              file=sys.stderr)
        sys.exit(2)

    for root in roots:
        if not root.exists(): continue
        for rec in iter_prompts(root, project_filter=args.project):
            if args.output_format == 'jsonl':
                print(json.dumps(rec, ensure_ascii=False))
            else:
                ts = rec.get('ts') or ''
                # Collapse whitespace so a record stays on one tab-separated line
                text = ' '.join(rec['text'].split())
                print(f"{rec['path']}\t{ts}\t{text}")

if __name__ == '__main__':
    _main()
