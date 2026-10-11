# Issue #7: realistic container failures

Goal: demonstrate deterministic repair of container configuration faults and stop
without redeployment when runtime configuration or external services need human
action. Preserve the existing `ErrorCategory` contract.

## Execution plan

1. Recover existing samples and measured deployer stderr; inspect the actual
   working tree before treating earlier task results as complete.
2. Add six Flask/gunicorn scenarios: chained bind, unreachable database,
   missing native runtime library, non-root write permission, missing required
   environment variables, and a crash with buffered output.
3. Extend fixture tests and implement rules for omitted bind addresses, native
   libraries in the final stage, and narrowly scoped write permissions.
4. Apply configuration/service/crash stops before either LLM fallback, including
   the repo-agent path. Check causality so caught warnings do not mask a later
   application bug.
5. Review log-derived shell arguments, ownership scope, runtime-stage/base-image
   assumptions, credential-free summaries, and repeated Dockerfile patches.
6. Run backend tests and lint. Deploy all six new and five existing broken apps
   with local Docker in isolated copies, run `heal()`, and verify HTTP health for
   successful repairs. Verify intended stops make no repair or redeploy calls.

## Acceptance evidence

- Every new scenario has app source, Dockerfile, requirements, a README row,
  and captured local deployer stderr.
- Fixture tests prove both classification and repair/stop behavior.
- Chained bind, native library, and permission scenarios repair successfully.
- DB startup (including slow preparation), missing env, and quiet crash stop
  before the repo agent or Dockerfile LLM can run.
- Existing five broken samples retain their deterministic outcomes.
- No credentials are embedded in samples. Permission fixes never widen system
  directory ownership or interpolate arbitrary log text into shell commands.
- Cloud Run deployment is optional and excluded from this local verification.

Implementation and verification remain local; this plan does not require a
commit, push, or deployment to a shared cloud environment.

## Local Docker results (2026-10-11)

Actual deployer failures were captured in seven fixtures (six samples plus the
slow DB preparation variant). Each E2E used a fresh source copy, disabled tunnels
and DB provisioning, and prohibited model callbacks. All eleven outcomes passed.

| Sample | Outcome | Redeploys | HTTP `/`, `/health` |
|---|---|---:|---|
| broken-bind-chain | healed | 1 | 200, 200 |
| broken-native-lib | healed | 1 | 200, 200 |
| broken-permission | healed | 1 | 200, 200 |
| broken-db-startup | service stop | 0 | server never starts |
| broken-missing-env | configuration stop | 0 | server never starts |
| broken-quiet-crash | crash stop | 0 | server never starts |
| broken | healed | 2 | 200, 200 |
| broken-port | healed | 1 | 200, 200 |
| broken-requirements | healed | 1 | 200, 200 |
| broken-import | app-code stop | 0 | server never starts |
| broken-health | app-code stop | 0 | original health endpoint fails |

The three new stop scenarios and slow DB variant also passed with a repo-repair
callback that raises if invoked: zero repair calls and zero redeploys. Existing
application-code stops were tested with the repo agent disabled. No Cloud Run or
live model validation was performed. Verification containers were removed.
A reproducible local Docker verification runner is available at `scripts/verify-sample-apps.py`.

Final backend checks: `uv run pytest -q` passed with 393 passed and 7 skipped;
focused healer/fixture tests passed with 206 passed. Ruff check, formatting check,
Python compilation, and `git diff --check` passed. Cloud Run startup log wrappers,
adversarial optional/terminal error sequences, and database-client error variants
(password auth, timeout, missing database, network unreachable) are covered by local tests.
