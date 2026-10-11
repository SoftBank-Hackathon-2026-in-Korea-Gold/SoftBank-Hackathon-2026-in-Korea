#!/usr/bin/env bash
# Repeatable local validation; cloud deployment is never started by this workflow.
set -uo pipefail

main() {
  repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
  run_docker=0
  run_browser=1
  run_tracing=0
  for argument in "$@"; do
    case "$argument" in
      --docker) run_docker=1 ;;
      --no-browser) run_browser=0 ;;
      --tracing) run_tracing=1 ;;
      -h|--help)
        echo 'Usage: scripts/next-steps-workflow.sh [--docker] [--tracing] [--no-browser]'
        echo 'Requires uv sync and npm ci; browser checks require Playwright Chromium.'
        exit 0 ;;
      *) echo "Unknown option: $argument" >&2; exit 2 ;;
    esac
  done
  run_dir="$(mktemp -d "${TMPDIR:-/tmp}/cloudmorph-next-steps.XXXXXX")"
  failures=0
  echo "Workflow evidence: $run_dir"

  check() {
    local label="$1"
    shift
    if "$@" > "$run_dir/$label.log" 2>&1; then
      echo "PASS $label"
    else
      local check_exit_code=$?
      echo "FAIL $label (exit $check_exit_code): $run_dir/$label.log" >&2
      tail -n 30 "$run_dir/$label.log" >&2
      failures=$((failures + 1))
    fi
  }

  cd "$repo_root/backend" || exit 2
  check backend-lint uv run --frozen ruff check app tests
  check backend-format uv run --frozen ruff format --check app tests
  check backend-tests uv run --frozen pytest -q
  if (( run_tracing )); then
    check backend-tracing uv run --frozen --extra tracing pytest -q tests/test_llmtrace.py
  fi
  cd "$repo_root/frontend" || exit 2
  check frontend-tests npm test
  check frontend-lint npm run lint
  check frontend-build npm run build
  if (( run_browser )); then
    check frontend-browser npm run test:e2e
  fi
  cd "$repo_root" || exit 2
  if (( run_docker )); then
    # The verifier uses copied source and cleans up only its own deployment handles.
    # Never use Docker prune: a demo may already be running on this host.
    check docker-samples env DEPLOYER_TUNNEL=0 CLOUDMORPH_HEAL_MEMORY=0 \
      LANGFUSE_TRACING_ENABLED=false uv run --frozen --project backend python scripts/verify-sample-apps.py --all
  fi
  echo "Workflow finished: $failures failed stage(s). Evidence: $run_dir"
  (( failures == 0 ))
}

# Parse the full workflow before executing its long-running stages.
main "$@"
