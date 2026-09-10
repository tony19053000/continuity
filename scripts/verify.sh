#!/usr/bin/env bash
# Run every gate check and print one compact line each.
#
# Exists to keep review cheap. Reviewers read this summary instead of running
# eight commands and paging through verbose output; failures print their detail,
# successes print a single line. Use --full to see everything.
set -uo pipefail
cd "$(dirname "$0")/.."

FULL=0
[[ "${1:-}" == "--full" ]] && FULL=1

FAILED=0
run() {
  local name="$1"; shift
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [[ $rc -eq 0 ]]; then
    # Pick the line that actually carries the result, not whatever printed last
    # (pytest ends on a progress line, npm on a banner).
    local summary
    summary="$(grep -aoE '[0-9]+ (passed|failed)[^|]*|Success: [^|]*|All checks passed!|Tests +[0-9]+ passed[^)]*\)|Compiled successfully[^ ]*|No new upgrade operations detected\.' <<<"$out" | tail -1)"
    [[ -z "$summary" ]] && summary="ok"
    printf '  PASS  %-22s %s\n' "$name" "$(cut -c1-72 <<<"$summary")"
  else
    printf '  FAIL  %-22s\n' "$name"
    sed 's/^/        /' <<<"$out" | tail -25
    FAILED=1
  fi
  [[ $FULL -eq 1 && $rc -eq 0 ]] && sed 's/^/        /' <<<"$out" | tail -15
  return 0
}

echo "=== backend ==="
run "pytest"            uv run pytest -p no:cacheprovider
run "ruff"              uv run ruff check . --output-format=concise
run "mypy"              uv run mypy backend
run "alembic check"     uv run alembic check

echo "=== frontend ==="
cd apps/web
run "tsc"               npm run typecheck
run "eslint"            npm run lint
run "vitest"            npm run test
run "next build"        npm run build
cd ../..

echo
if [[ $FAILED -eq 0 ]]; then
  echo "ALL CHECKS PASS"
else
  echo "SOME CHECKS FAILED"
fi
exit $FAILED
