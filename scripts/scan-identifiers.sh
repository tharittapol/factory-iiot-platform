#!/usr/bin/env bash
# Fail if any tracked file contains a customer identifier.
#
# The patterns are deliberately not in the repo: a list of identifiers to keep
# out would itself publish them. They come from, in order:
#   1. $SCAN_PATTERNS   CI, from the SCAN_PATTERNS Actions secret
#   2. .scan-patterns   local, gitignored, one extended regex per line
# A hit is reported by file name only, so it never prints the identifier into
# a public CI log.
set -euo pipefail

cd "$(dirname "$0")/.."

patterns=$(mktemp)
trap 'rm -f "$patterns"' EXIT

if [ -n "${SCAN_PATTERNS:-}" ]; then
  printf '%s\n' "$SCAN_PATTERNS" > "$patterns"
elif [ -f .scan-patterns ]; then
  cat .scan-patterns > "$patterns"
fi

# A blank pattern matches every line in the tree; drop blanks and comments.
sed -i -E '/^[[:space:]]*(#|$)/d' "$patterns"

if [ ! -s "$patterns" ]; then
  if [ -n "${CI:-}" ]; then
    echo "::error::SCAN_PATTERNS secret is not set; refusing to pass without patterns"
    exit 1
  fi
  echo "  skipped (no patterns configured: create .scan-patterns)"
  exit 0
fi

if files=$(git grep -lIiE -f "$patterns" -- .); then
  echo "customer identifier found in:"
  echo "$files" | sed 's/^/  /'
  exit 1
fi
