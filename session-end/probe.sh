#!/usr/bin/env sh
# session-end evidence probe: everything Step 1 of SKILL.md needs, in one call.
# usage: sh probe.sh "<session start, e.g. 2026-09-16 09:00, or anything git's --since accepts>"   (default: 12 hours ago)
# Read-only. Prints sections; never writes, commits, fetches, or changes a checkout.
SINCE="${1:-12 hours ago}"
# git reads SINCE itself, so the caller's window is never replaced. Only the pull-request search needs a calendar
# day, found with whichever date tool this system has: GNU date, then BSD date (macOS), then a leading YYYY-MM-DD.
day_of() {
  date -d "$1" +%F 2>/dev/null && return 0
  for f in '%Y-%m-%d %H:%M' '%Y-%m-%d %H:%M:%S' '%Y-%m-%dT%H:%M' '%Y-%m-%dT%H:%M:%S' '%Y-%m-%d'; do
    date -j -f "$f" "$1" +%F 2>/dev/null && return 0
  done
  case "$1" in [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*) printf '%s\n' "$1" | cut -c1-10; return 0 ;; esac
  return 1
}
SINCE_DAY=$(day_of "$SINCE")
if [ -z "$SINCE_DAY" ]; then
  SINCE_DAY=$(date -d yesterday +%F 2>/dev/null || date -v-1d +%F 2>/dev/null || date +%F)
  echo "WARNING: could not turn '$SINCE' into a calendar day; git still uses it as given, and the pull-request search starts at $SINCE_DAY."
fi
sec() { printf '\n== %s ==\n' "$1"; }

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  sec "not a git repository"
  pwd
  echo "No repo here: skip the handoff file and emit the record in chat only (Step 4)."
  exit 0
fi

PRIMARY=$(git worktree list --porcelain | head -1 | sed 's/^worktree //')
BARE=$(git worktree list --porcelain | sed -n '2p' | grep -c '^bare$')
TOP=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
[ "$BRANCH" = HEAD ] && BRANCH="detached@$(git rev-parse --short HEAD 2>/dev/null)"
DEFAULT=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')
if [ -z "$DEFAULT" ]; then
  if git show-ref --verify --quiet refs/remotes/origin/main; then DEFAULT=main; else DEFAULT=master; fi
fi
WT=$(basename "$TOP")

sec "where"
echo "primary checkout: $PRIMARY$([ "$BARE" = 1 ] && echo '   (bare repository)')"
echo "this checkout:    $TOP"
echo "branch:           $BRANCH   (default branch: $DEFAULT)"
echo "window:           since $SINCE  ($SINCE_DAY)"
[ -n "$CLAUDE_SESSION_ID" ] && echo "session id:       $CLAUDE_SESSION_ID"

sec "working tree: git status --short (first 60)"
git status --short | head -60
sec "uncommitted diff stat"
git diff --stat | tail -25
sec "last 15 commits on $BRANCH"
git log --oneline -15
sec "commits in the window on $BRANCH, with the files each touched"
git log --since="$SINCE" --format='%h %ad %an | %s' --date=short --name-status | head -150
sec "commits in the window on origin/$DEFAULT, with the files each touched (as last fetched; this probe does not fetch)"
git log "origin/$DEFAULT" --since="$SINCE" --format='%h %ad %an | %s' --date=short --name-status 2>/dev/null | head -150
sec "this branch against origin/$DEFAULT: files changed since the merge base"
git diff --stat "origin/$DEFAULT...HEAD" 2>/dev/null | tail -25

sec "pull requests by this author, merged since $SINCE_DAY (limit 100)"
if command -v gh >/dev/null 2>&1; then
  MERGED=$(gh pr list --state merged --author @me --search "merged:>=$SINCE_DAY" --limit 100 \
    --json number,title,headRefName,mergedAt \
    -q '.[] | "#\(.number) \(.mergedAt[0:16]) \(.headRefName) | \(.title)"' 2>&1)
  if [ -n "$MERGED" ]; then printf '%s\n' "$MERGED"; else echo "(none)"; fi
  if [ "$(printf '%s\n' "$MERGED" | grep -c '^#')" -ge 100 ]; then
    echo "WARNING: hit the limit of 100. Raise it before reporting a count."
  fi
  echo "Same author is not same session: match each by branch, timing or content before claiming it."
  sec "open pull requests by this author, updated since $SINCE_DAY (older open ones are not this session's)"
  OPEN=$(gh pr list --state open --author @me --search "updated:>=$SINCE_DAY" --limit 30 --json number,title,headRefName,updatedAt \
    -q '.[] | "#\(.number) \(.updatedAt[0:16]) \(.headRefName) | \(.title)"' 2>&1)
  if [ -n "$OPEN" ]; then printf '%s\n' "$OPEN"; else echo "(none)"; fi
else
  echo "gh is not installed: could not list pull requests. Say so in the record; do not report none."
fi

sec "handoff target"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
TOKEN=$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')
echo "write to:     $PRIMARY/.claude/handoffs/${STAMP}_<slug>_${TOKEN}.md"
echo "line 2 stamp: <!-- session-end:origin branch=$BRANCH worktree=$WT -->"
if [ "$BARE" = 1 ]; then
  echo "tracking:     primary is a bare repository; no ignore check applies."
elif git -C "$PRIMARY" check-ignore -q .claude/handoffs/probe.md 2>/dev/null; then
  echo "tracking:     .claude/handoffs is ignored there (durable, untracked)."
else
  rc=$?
  if [ "$rc" = 1 ]; then echo "tracking:     .claude/handoffs is not ignored there (the project may track handoffs)."
  else echo "tracking:     could not check (git check-ignore exit $rc)."; fi
fi

sec "close-out contract"
CONTRACT="$PRIMARY/.claude/session-close-out.md"
if [ -f "$CONTRACT" ]; then
  echo "PRESENT at $CONTRACT: Step 4b applies. Contents:"
  head -150 "$CONTRACT"
else
  echo "none declared: skip Step 4b."
fi
