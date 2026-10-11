#!/usr/bin/env python3
"""Reproducible verification runner for sample-apps self-healing and early stops.

Deploys sample apps with local Docker in isolated temporary directories,
runs healer.heal(), verifies HTTP health for healed apps (200 on / and /health),
and verifies that unhealable scenarios stop cleanly without redeployment or model fallback.

Usage:
    uv run python scripts/verify-sample-apps.py            # runs 6 issue #7 scenarios
    uv run python scripts/verify-sample-apps.py --all      # runs all 11 broken sample apps
    uv run python scripts/verify-sample-apps.py <name>     # runs a single sample app
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app import deployer, healer
from app.healer import (
    APP_CODE_STOP,
    CONFIG_STOP,
    CRASH_STOP,
    SERVICE_STOP,
)

SAMPLE_APPS = ROOT / "sample-apps"

# Expected test contract: (expected_outcome, expected_stop_prefix_or_none)
SCENARIOS: dict[str, tuple[str, str | None]] = {
    # Issue #7 realistic scenarios
    "broken-bind-chain": ("healed", None),
    "broken-native-lib": ("healed", None),
    "broken-permission": ("healed", None),
    "broken-db-startup": ("service stop", SERVICE_STOP),
    "broken-missing-env": ("configuration stop", CONFIG_STOP),
    "broken-quiet-crash": ("crash stop", CRASH_STOP),
    # Pre-existing broken samples
    "broken": ("healed", None),
    "broken-port": ("healed", None),
    "broken-requirements": ("healed", None),
    "broken-import": ("app-code stop", APP_CODE_STOP),
    "broken-health": ("app-code stop", APP_CODE_STOP),
}

DEFAULT_ISSUE7_SAMPLES = [
    "broken-bind-chain",
    "broken-native-lib",
    "broken-permission",
    "broken-db-startup",
    "broken-missing-env",
    "broken-quiet-crash",
]


def _forbidden_agent(*_):
    raise AssertionError("repo agent / LLM fallback must not be called")


def verify_sample(name: str) -> tuple[bool, str, int, str]:
    """Runs a single sample app in an isolated directory and returns:

    (passed, outcome_str, redeploy_count, detail_str)
    """
    if name not in SCENARIOS:
        print(f"Unknown scenario: {name}", file=sys.stderr)
        return False, "unknown", 0, "Scenario not defined"

    expected_outcome, expected_stop_prefix = SCENARIOS[name]
    src_path = SAMPLE_APPS / name
    if not src_path.is_dir():
        return False, "missing", 0, f"Directory not found: {src_path}"

    with tempfile.TemporaryDirectory() as tmp_dir:
        # Copy to isolated directory
        shutil.copytree(src_path, tmp_dir, dirs_exist_ok=True)
        dockerfile = (Path(tmp_dir) / "Dockerfile").read_text()
        timeout = 10 if name == "broken-db-startup" else 15
        redeploy_count = 0
        summary = ""

        def redeploy(s, d, t):
            nonlocal redeploy_count
            redeploy_count += 1
            return deployer.deploy(s, d, t, tunnel=False, verify_timeout=timeout)

        passed = False
        detail = ""
        # One failing sample (assertion, HTTP error, Docker error) is recorded, not fatal, and its
        # containers are always cleaned up.
        try:
            initial_res = deployer.deploy(tmp_dir, dockerfile, "local", tunnel=False, verify_timeout=timeout)
            # Issue #7 human-stop scenarios must never invoke the repo agent even if provided.
            # Pre-existing app-code stops (broken-import, broken-health) run with repo agent disabled (None).
            # A non-None llm_patch_fn also keeps heal() from enabling the repo agent when ANTHROPIC_API_KEY
            # is exported, so no sample can make a model call.
            repair_fn = _forbidden_agent if name in DEFAULT_ISSUE7_SAMPLES else None
            report = healer.heal(
                tmp_dir,
                "local",
                initial_res,
                dockerfile,
                redeploy,
                llm_patch_fn=_forbidden_agent,
                repair_fn=repair_fn,
            )
            summary = report.summary

            if expected_outcome == "healed":
                if not report.success:
                    detail = f"Failed to heal: {report.summary}"
                elif not report.final_url:
                    detail = "Healed but no URL returned"
                else:
                    # Verify both / and /health return HTTP 200
                    base_url = report.final_url.rstrip("/")
                    with urllib.request.urlopen(f"{base_url}/", timeout=5) as r1:
                        s1 = r1.status
                    with urllib.request.urlopen(f"{base_url}/health", timeout=5) as r2:
                        s2 = r2.status
                    if s1 == 200 and s2 == 200:
                        passed = True
                        detail = f"{s1}, {s2}"
                    else:
                        detail = f"HTTP {s1}, {s2}"
            else:
                if report.success:
                    detail = "Expected stop but heal reported success"
                elif report.attempts != 0:
                    detail = f"Expected 0 redeploys but attempted {report.attempts}"
                elif not (expected_stop_prefix and report.summary.startswith(expected_stop_prefix)):
                    detail = f"Summary mismatch: {report.summary}"
                else:
                    passed = True
                    detail = "stopped cleanly (0 redeploys)"
        except Exception as exc:  # noqa: BLE001 - record any failure for this sample and move on
            summary = summary or f"{type(exc).__name__}: {exc}"
            detail = f"{type(exc).__name__}: {exc}"
        finally:
            deployer.cleanup(tmp_dir)

        return (
            passed,
            expected_outcome if passed else f"failed ({summary[:30]})",
            redeploy_count,
            detail,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="Run all 11 broken sample apps")
    parser.add_argument("sample", nargs="?", help="Specific sample name to verify")
    args = parser.parse_args()

    if args.sample:
        targets = [args.sample]
    elif args.all:
        targets = list(SCENARIOS.keys())
    else:
        targets = DEFAULT_ISSUE7_SAMPLES

    print(f"\n[verify] Running verification for {len(targets)} sample(s)...\n")
    print(f"{'Sample':<25} | {'Outcome':<18} | {'Redeploys':<9} | {'Detail / HTTP'}")
    print("-" * 75)

    all_passed = True
    results = []

    for name in targets:
        print(f"Running {name}...", end="\r", flush=True)
        passed, outcome, redeploys, detail = verify_sample(name)
        status_marker = "✓" if passed else "✗"
        print(f"{status_marker} {name:<23} | {outcome:<18} | {redeploys:<9} | {detail}")
        results.append((name, passed, outcome, redeploys, detail))
        if not passed:
            all_passed = False

    print("\n" + "=" * 75)
    if all_passed:
        print(f"All {len(targets)} verification check(s) PASSED!")
        return 0
    else:
        failed_count = sum(1 for _, p, _, _, _ in results if not p)
        print(f"{failed_count} verification check(s) FAILED!", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
