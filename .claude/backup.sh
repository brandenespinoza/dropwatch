#!/bin/sh
# Back the repository up to GitHub.
#
# Run automatically by the Stop hook in .claude/settings.json every time a
# Claude Code session finishes, so a set of changes is never left only on this
# machine. Safe to run by hand at any time; it does nothing when the working
# tree is clean and the branch is already pushed.
#
#   .claude/backup.sh              commit anything new, then push
#   .claude/backup.sh --dry-run    report what it would do, change nothing

set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 0

DRY_RUN=no
[ "${1:-}" = "--dry-run" ] && DRY_RUN=yes

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" || exit 0
[ -n "$BRANCH" ] && [ "$BRANCH" != HEAD ] || exit 0

git add -A 2>/dev/null

# Nothing new to record is the common case; a push may still be owed from a
# previous run that could not reach the network.
if git diff --cached --quiet 2>/dev/null; then
    COMMITTED=""
else
    SUBJECT="Save work in progress — $(date '+%Y-%m-%d %H:%M')"
    if [ "$DRY_RUN" = yes ]; then
        echo "would commit: $SUBJECT"
        echo "would push:   $BRANCH -> origin/$BRANCH"
        exit 0
    fi
    git commit -q -m "$SUBJECT" 2>/dev/null || exit 0
    COMMITTED=yes
fi

AHEAD="$(git rev-list --count "origin/$BRANCH..$BRANCH" 2>/dev/null || echo 0)"

if [ "$DRY_RUN" = yes ]; then
    [ -z "$COMMITTED" ] && echo "nothing to commit"
    echo "would push $AHEAD commit(s): $BRANCH -> origin/$BRANCH"
    exit 0
fi

[ "$AHEAD" -gt 0 ] 2>/dev/null || exit 0

if git push -q origin "$BRANCH" 2>/dev/null; then
    printf '{"systemMessage":"Backed up %s commit(s) to GitHub (%s)."}\n' "$AHEAD" "$BRANCH"
else
    printf '{"systemMessage":"Could not reach GitHub — %s commit(s) saved locally, will push next time."}\n' "$AHEAD"
fi

exit 0
