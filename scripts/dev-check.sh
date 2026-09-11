#!/usr/bin/env bash
# Run the same gates as CI, locally, before pushing.
set -euo pipefail

cd "$(dirname "$0")/.."
fail=0
step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
try()  { if "$@"; then echo "  ok"; else echo "  FAILED"; fail=1; fi; }

step "lint"
try bash -c 'cd services/plc-sim && uv run ruff check .'

step "tests"
try bash -c 'cd services/plc-sim && uv run pytest -q'

step "config contract"
try bash -c 'cd services/plc-sim && uv run python -c "
from plc_sim.config import load_config
c = load_config(\"config/tags.yaml\")
print(f\"  version={c.version} tags={len(c.tags)}\")
"'

step "secret scan"
if git ls-files | grep -iE '\.(db|sqlite3?|pem|crt|key)$'; then
  echo "  FAILED: credential or binary file tracked"; fail=1
else
  echo "  ok"
fi

step "customer identifiers"
if git grep -rIn -iE 'REDACTED' -- . ':!.github/workflows/ci.yml' ':!Makefile' ':!scripts/dev-check.sh' >/dev/null 2>&1; then
  echo "  FAILED: customer identifier present"; fail=1
else
  echo "  ok"
fi

step "systemd unit"
if command -v systemd-analyze >/dev/null; then
  if systemd-analyze verify deploy/systemd/plc-sim@.service 2>&1 | grep -q "Unknown key name"; then
    echo "  FAILED: misplaced directive"; fail=1
  else
    echo "  ok"
  fi
else
  echo "  skipped (systemd-analyze not available)"
fi

printf '\n'
if [ "$fail" -ne 0 ]; then echo "some checks failed"; exit 1; fi
echo "all checks passed"
