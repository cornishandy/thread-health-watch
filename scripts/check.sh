#!/usr/bin/env bash
# Read-only checks to run before every commit and push. Exit 0 only when everything passes.
#   1. each mirrored file matches its live copy (scripts/live-paths.txt);
#   2. only files on scripts/allowlist.txt would be published;
#   3. no private data in those files, their names, the staged changes, or anywhere in the git history;
#   4. gitleaks finds no secrets in the tree or the history.
# Words that must never appear live outside the repo, so this script never spells them out: one extended
# regex per line (case-sensitive) in $THW_PRIVATE_PATTERNS, default
# ~/.config/thread-health-watch/private-patterns.txt. A missing or empty file fails the check.
# Hits print as a pattern number and path:line, never the matched text.
set -u
cd "$(dirname "$0")/.." || exit 2
fail=0
bad() { echo "  $*"; fail=1; }
has_refs() { [ -n "$(git for-each-ref --count=1)" ]; }  # any branch, tag or T3 checkpoint

echo "== 1. Repo matches the live copies"
n=0
while read -r repo live _; do
  case "$repo" in '' | \#*) continue ;; esac
  live="${live/#\~/$HOME}"
  n=$((n + 1))
  if [ ! -f "$live" ]; then bad "no live copy: $repo"
  elif ! cmp -s "$repo" "$live"; then bad "differs from live: $repo"
  fi
done < scripts/live-paths.txt
echo "  $n files compared"

echo "== 2. Only allowlisted files"
files=()
while IFS= read -r f; do [ -f "$f" ] && files+=("$f"); done < <(git ls-files -co --exclude-standard)
for f in "${files[@]}"; do
  grep -qxF -- "$f" scripts/allowlist.txt || bad "not on scripts/allowlist.txt: $f"
done
ignored=$(git ls-files -ci --exclude-standard)
[ -n "$ignored" ] && bad "tracked even though .gitignore marks it private: $ignored"
echo "  ${#files[@]} files"

echo "== 3. Private data"
patterns="${THW_PRIVATE_PATTERNS:-$HOME/.config/thread-health-watch/private-patterns.txt}"
if [ ! -s "$patterns" ]; then
  bad "owner pattern file missing or empty: $patterns"
else
  i=0
  while IFS= read -r pat; do
    i=$((i + 1))
    [ -z "$pat" ] && continue
    hits=$(grep -n -E -- "$pat" "${files[@]}" | cut -d: -f1,2 | tr '\n' ' ')
    [ -n "$hits" ] && bad "pattern $i in: $hits"
    printf '%s\n' "${files[@]}" | grep -q -E -- "$pat" && bad "pattern $i in a file name"
    git diff --cached | grep -q -E -- "$pat" && bad "pattern $i in the staged changes"
    if has_refs; then
      git log --all -p --format='commit %H%n%an <%ae>%n%cn <%ce>%n%B' | grep -q -E -- "$pat" \
        && bad "pattern $i in the git history (content, authors or messages)"
      git log --all --format= --name-only | grep -q -E -- "$pat" && bad "pattern $i in a file name in the history"
    fi
  done < "$patterns"
fi
# Generic leaks, built from pieces so this file doesn't flag itself.
home='/'"Users"'/|/home/[a-z]'
tailnet='\.ts\.net\b|\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b'
hooks='relay\.t3\.codes|hooks\.slack\.com|discord(app)?\.com/api/webhooks'
generic="$home|$tailnet|$hooks"
email='[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
okmail='@(users\.noreply\.github\.com|example\.invalid|example\.com|anthropic\.com)$'
hits=$(grep -n -E "$generic" -- "${files[@]}" | cut -d: -f1,2 | tr '\n' ' ')
[ -n "$hits" ] && bad "home paths, tailnet hosts or webhook URLs in: $hits"
emails=$(grep -n -o -E "$email" -- "${files[@]}" | grep -v -E "$okmail" | cut -d: -f1,2 | tr '\n' ' ')
[ -n "$emails" ] && bad "email addresses in: $emails"
# The same generic checks on what would be committed (the index) and on every commit already made.
leaky() { local text; text=$(cat); grep -q -E "$generic" <<< "$text" && return 0
  grep -o -E "$email" <<< "$text" | grep -q -v -E "$okmail"; }
git diff --cached | leaky && bad "home paths, hosts, webhook URLs or emails in the staged changes"
if has_refs; then
  git log --all -p --format='commit %H%n%an <%ae>%n%cn <%ce>%n%B' | leaky \
    && bad "home paths, hosts, webhook URLs or emails in the git history (content, authors or messages)"
fi
if has_refs; then
  others=$(git log --all --format='%ae%n%ce' | sort -u | grep -v -E '@users\.noreply\.github\.com$|^noreply@')
  [ -n "$others" ] && bad "a commit by an address that isn't a noreply one"
fi

echo "== 4. gitleaks"
if command -v gitleaks > /dev/null; then
  gitleaks dir . --no-banner --redact > /dev/null 2>&1 || bad "gitleaks found something in the tree: run gitleaks dir . --redact -v"
  if has_refs; then
    gitleaks git . --no-banner --redact > /dev/null 2>&1 || bad "gitleaks found something in the history: run gitleaks git . --redact -v"
  fi
else
  bad "gitleaks is not installed"
fi

[ "$fail" = 0 ] && echo "ALL CLEAR" || echo "CHECK FAILED"
exit "$fail"
