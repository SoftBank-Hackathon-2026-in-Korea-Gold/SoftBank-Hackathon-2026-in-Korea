"""Deployer (owner: 전동훈).

Build the user's app into a container and run it on `local` (Docker + Cloudflare quick tunnel)
or `cloudrun` (Artifact Registry + Cloud Run). Contract: app/schemas.py::DeployResult, docs/interfaces.md.

    deploy(src_dir, dockerfile, target) -> DeployResult   # never raises on build/run failure

Pipeline (each stage is recorded; the first failing stage decides `stderr`):

    preflight -> build -> [push] -> deploy -> expose -> verify
    cloudrun redeploys are blue/green: a --no-traffic `candidate` revision is verified before
    promotion (--to-revisions=<verified>=100). First deployments have no prior revision to protect
    and receive traffic before verification; a failed first probe is reported as a failure.

`stderr` layout (what healer classifies):

    [deployer] stage=<stage> kind=<kind> target=<target>
    <one-line diagnosis>
    --- error ---            CLI stderr of the failing command
    --- runtime logs ---     `docker logs` / Cloud Run revision logs (always included when a container ran)

Environment (all optional):
    GCP_PROJECT_ID, GCP_REGION (asia-northeast3), GCP_AR_REPO (hackathon)
    DEPLOYER_TUNNEL=0            disable cloudflared for local (CI / tests)
    DEPLOYER_HEALTH_PATH=/health health path used by verify (falls back to "/")
    DEPLOYER_PROBE_PATH=/health  if set, Cloud Run uses an HTTP startup probe on this path (default: TCP)
    DEPLOYER_VERIFY_TIMEOUT=60   seconds to wait for the app to answer
    DEPLOYER_SERVICE_NAME        Cloud Run service name (default: slug of src_dir basename)
    DEPLOYER_APP_SERVICE_ACCOUNT runtime identity of deployed cloudrun/function apps ("name" -> name@<project>.iam…).
                                 Unset -> the project's default compute account (Editor!) — set it on any shared server.

CLI (manual testing):
    uv run python -m app.deployer <src_dir> local|cloudrun [--dockerfile PATH] [--no-tunnel] [--service NAME]
    uv run python -m app.deployer cleanup <src_dir>
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app import nodes as nodepool
from app.schemas import DeployResult, DeployTarget

TAIL = 6000  # max chars kept per captured stream
STATE_DIR = ".deployer"  # written inside src_dir: last outcome + tunnel logs


# --------------------------------------------------------------------------- #
# Internal models
# --------------------------------------------------------------------------- #
@dataclass
class Step:
    name: str
    cmd: str
    returncode: int | None
    duration: float
    stdout: str
    stderr: str


@dataclass
class Outcome:
    """Rich internal result; `to_contract()` flattens it into schemas.DeployResult."""

    target: str
    stage: str = "preflight"
    ok: bool = False
    url: str | None = None
    kind: str | None = (
        None  # user_action_required | build_error | push_error | deploy_error | runtime_error | timeout
    )
    error: str | None = None  # raw stderr of the failing command
    app_logs: str | None = None  # raw container / revision logs
    diagnosis: str | None = None
    steps: list[Step] = field(default_factory=list)
    handles: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    elapsed: float = 0.0

    def exit_code(self) -> int:
        if self.ok:
            return 0
        for step in reversed(self.steps):
            if step.returncode not in (0, None):
                return step.returncode
        return 1

    def stderr_text(self) -> str:
        if self.ok:
            return "\n".join(self.warnings)
        parts = [f"[deployer] stage={self.stage} kind={self.kind} target={self.target}"]
        if size := self.handles.get("size"):  # healer sizes its memory patch from this
            parts.append("[deployer] instance " + " ".join(f"{k}={v}" for k, v in size.items()))
        if self.diagnosis:
            parts.append(self.diagnosis)
        if self.error:
            parts += ["--- error ---", self.error.rstrip()]
        if self.app_logs:
            parts += ["--- runtime logs ---", self.app_logs.rstrip()]
        return "\n".join(parts)

    def stdout_text(self) -> str:
        lines = [f"{s.name}: rc={s.returncode} {s.duration:.1f}s" for s in self.steps]
        for s in self.steps:
            if s.name in ("docker build", "gcloud run deploy") and s.stdout.strip():
                lines += [f"--- {s.name} stdout ---", s.stdout.rstrip()]
        return "\n".join(lines)[-TAIL:]

    def to_contract(self) -> DeployResult:
        return DeployResult(
            success=self.ok,
            target=self.target,  # type: ignore[arg-type]
            exit_code=self.exit_code(),
            stdout=self.stdout_text(),
            stderr=self.stderr_text(),
            url=self.url if self.ok else None,
            duration_sec=round(self.elapsed, 1),
        )


@dataclass
class Config:
    project: str | None = None
    region: str = "asia-northeast3"
    repo: str = "hackathon"
    service: str | None = None
    tag: str | None = None
    container_port: int = 8080
    health_path: str = "/health"
    tunnel: bool = True
    probe_path: str | None = None  # None -> TCP startup probe (Cloud Run default behaviour)
    allow_unauthenticated: bool = True
    rollback_on_verify_failure: bool = True
    verify_timeout: int = 60
    deploy_timeout: int = 600
    build_timeout: int = 900
    skip_preflight: bool = False
    env: dict = field(
        default_factory=dict
    )  # extra env vars injected into the app container (e.g. DATABASE_URL)
    build_args: dict = field(default_factory=dict)  # docker build --build-arg (non-secret build inputs)
    # BuildKit secrets (RUN --mount=type=secret,id=NAME): handed over via the build's process env,
    # so the values never appear on the command line, in the state file, or in image layers
    build_secrets: dict = field(default_factory=dict)
    node_arch: str | None = None  # restrict node pool to an architecture; None -> any
    node_exclude: tuple = ()  # node names to skip (used by fleet scale-out)
    database: str | None = "auto"  # "auto": Postgres when the app reads DATABASE_URL; "postgres"; None: never
    cloudsql_instance: str = "cloudmorph-pg"  # Cloud SQL instance (cloudrun target)
    # Runtime identity of user apps on cloudrun/function. Untrusted code can read its token from the
    # metadata server, so on a shared server this must be an account with no roles beyond cloudsql.client.
    app_service_account: str | None = None
    # create the instance when it is missing (~10 min, billed while it exists)
    cloudsql_autocreate: bool = False
    cloudsql_tier: str = "db-f1-micro"
    migrate: bool = True  # run the app's schema migrations (alembic, Django, prisma) before it starts
    cache: bool = True  # Redis sidecar (local/node) for an app that reads REDIS_URL
    db_secret: bool = (
        True  # cloudrun: keep the Cloud SQL DATABASE_URL in Secret Manager (CloudMorph reads it back)
    )
    # ...and mount it into the app instead of a plain env var. Only with one runtime account per app: the
    # account gets read access to the secret, and apps sharing DEPLOYER_APP_SERVICE_ACCOUNT would read each other's
    db_secret_mount: bool = False
    db_owner: str | None = None  # where the code comes from (main._source_identity): isolates shared DB names
    # instance size. cpu in vCPUs ("1", "2"), memory like "512Mi"/"1Gi"; None -> platform default.
    # Precedence: detected from dependencies < cpu/memory (env or override) < Dockerfile LABEL cloudmorph.*
    cpu: str | None = None
    memory: str | None = None
    min_instances: int | None = None  # cloudrun/function; None -> inherit (first deploy: 0)
    max_instances: int | None = None
    concurrency: int | None = None  # requests per instance
    request_timeout: int | None = None  # seconds
    db_max_instances: int = 4  # max_instances when a Cloud SQL database is attached and none is set

    def runtime_account(self) -> str | None:
        sa = self.app_service_account
        if sa and "@" not in sa and self.project:
            return f"{sa}@{self.project}.iam.gserviceaccount.com"
        return sa

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ
        return cls(
            project=env.get("GCP_PROJECT_ID") or None,
            region=env.get("GCP_REGION", "asia-northeast3"),
            repo=env.get("GCP_AR_REPO", "hackathon"),
            service=env.get("DEPLOYER_SERVICE_NAME") or None,
            health_path=env.get("DEPLOYER_HEALTH_PATH", "/health"),
            tunnel=env.get("DEPLOYER_TUNNEL", "1") not in ("0", "false", "no"),
            probe_path=env.get("DEPLOYER_PROBE_PATH") or None,
            verify_timeout=int(env.get("DEPLOYER_VERIFY_TIMEOUT", "60")),
            database=env.get("DEPLOYER_DATABASE", "auto") or None,
            cache=env.get("DEPLOYER_CACHE", "1") not in ("0", "false", "no", ""),
            migrate=env.get("DEPLOYER_MIGRATE", "1") not in ("0", "false", "no", ""),
            db_secret=env.get("DEPLOYER_DB_SECRET", "1") not in ("0", "false", "no", ""),
            db_secret_mount=env.get("DEPLOYER_DB_SECRET_MOUNT", "0") in ("1", "true", "yes"),
            cloudsql_instance=env.get("CLOUDSQL_INSTANCE", "cloudmorph-pg"),
            app_service_account=env.get("DEPLOYER_APP_SERVICE_ACCOUNT") or None,
            cloudsql_autocreate=env.get("CLOUDSQL_AUTOCREATE", "0") in ("1", "true", "yes"),
            cloudsql_tier=env.get("CLOUDSQL_TIER", "db-f1-micro"),
            cpu=env.get("DEPLOYER_CPU") or None,
            memory=env.get("DEPLOYER_MEMORY") or None,
            min_instances=_int_env("DEPLOYER_MIN_INSTANCES"),
            max_instances=_int_env("DEPLOYER_MAX_INSTANCES"),
            concurrency=_int_env("DEPLOYER_CONCURRENCY"),
            request_timeout=_int_env("DEPLOYER_REQUEST_TIMEOUT"),
            db_max_instances=_int_env("DEPLOYER_DB_MAX_INSTANCES") or 4,
        )


def _int_env(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    return int(value) if value else None


class StepError(Exception):
    def __init__(
        self, stage: str, kind: str, error: str, app_logs: str | None = None, diagnosis: str | None = None
    ):
        super().__init__(error)
        self.stage, self.kind, self.error, self.app_logs, self.diagnosis = (
            stage,
            kind,
            error,
            app_logs,
            diagnosis,
        )


# --------------------------------------------------------------------------- #
# Command runner
# --------------------------------------------------------------------------- #
class Runner:
    def __init__(self) -> None:
        self.steps: list[Step] = []

    def run(
        self,
        name: str,
        cmd: list[str],
        *,
        timeout: int = 300,
        check: bool = True,
        stage: str = "",
        kind: str = "deploy_error",
        env: dict | None = None,  # extra process env (not recorded in Step.cmd)
    ) -> subprocess.CompletedProcess:
        t0 = time.time()
        try:
            cp = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
                env={**os.environ, **env} if env else None,
            )
        except subprocess.TimeoutExpired as e:
            err = _as_text(e.stderr)
            self.steps.append(Step(name, " ".join(cmd), None, time.time() - t0, _as_text(e.stdout), err))
            raise StepError(stage, "timeout", f"'{name}' exceeded {timeout}s\n{err}") from None
        except FileNotFoundError:
            self.steps.append(Step(name, " ".join(cmd), None, time.time() - t0, "", f"not found: {cmd[0]}"))
            raise StepError(stage, "user_action_required", f"command not found: {cmd[0]}") from None
        self.steps.append(
            Step(name, " ".join(cmd), cp.returncode, time.time() - t0, cp.stdout[-TAIL:], cp.stderr[-TAIL:])
        )
        if check and cp.returncode != 0:
            raise StepError(stage, kind, (cp.stderr or cp.stdout)[-TAIL:])
        return cp


def _as_text(v: bytes | str | None) -> str:
    if v is None:
        return ""
    return (v.decode(errors="replace") if isinstance(v, bytes) else v)[-TAIL:]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40]
    return s or "app"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_status(url: str, timeout: float = 5.0) -> tuple[int | None, str]:
    """Return (status, label). status None means no HTTP answer at all."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return e.code, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 — URLError/timeout/reset all mean 'not up yet'
        return None, type(e).__name__


def wait_up(
    base_url: str, health_path: str, timeout: int, *, is_dead=None, interval: float = 2.0
) -> tuple[bool, str]:
    """Probe health first; a missing health route must fall back to the root route.

    Root 4xx still demonstrates liveness for API-only apps with no index page. A missing
    health endpoint alone says nothing about whether the application's routes work.
    """
    deadline = time.time() + timeout
    last = "not attempted"
    while time.time() < deadline:
        for path in (health_path, "/") if health_path != "/" else ("/",):
            status, last = http_status(base_url + path)
            if path != "/" and status in (404, 405):
                continue
            if status is not None and status < 500:
                return True, last
            if status is not None and status >= 500:
                break  # server answered with an error: don't bother with the fallback path this round
        if is_dead and is_dead():
            return False, f"process exited (last probe: {last})"
        time.sleep(interval)
    return False, f"timeout after {timeout}s (last probe: {last})"


_LISTENING = re.compile(
    r"Listening at:|listening on|Running on http|Server running|started server on", re.IGNORECASE
)
_CRASHED = re.compile(
    r"Traceback \(most recent call last\)|Error:|Exception|Worker failed to boot|exited with code",
    re.IGNORECASE,
)


def diagnose_not_reachable(port: int, why: str, logs: str | None, *, exited: bool | None = None) -> str:
    """One line for healer. Only call the problem a port/host binding issue when the app is actually
    alive and listening somewhere; a crashed worker also logs 'Listening at' (gunicorn master) first."""
    logs = logs or ""
    if exited:
        return f"container exited during startup ({why}); see runtime logs for the traceback"
    if "HTTP 5" in why:
        return f"app is listening on PORT={port} but answered an error ({why}); see runtime logs"
    if _LISTENING.search(logs) and not _CRASHED.search(logs):
        return (
            f"container started but failed to start and listen on the port defined by PORT={port} "
            f"({why}); the app is probably bound to another port or to 127.0.0.1"
        )
    return f"container did not answer on PORT={port} ({why}); see runtime logs"


def gcloud_default_project() -> str | None:
    try:
        cp = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return cp.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _write_state(src_dir: Path, outcome: Outcome) -> None:
    state = src_dir / STATE_DIR
    state.mkdir(exist_ok=True)
    (state / "last_result.json").write_text(json.dumps(asdict(outcome), ensure_ascii=False, indent=2))


def _read_handles(src_dir: Path) -> dict:
    try:
        return json.loads((src_dir / STATE_DIR / "last_result.json").read_text())["handles"]
    except (OSError, ValueError, KeyError):
        return {}


# --------------------------------------------------------------------------- #
# preflight
# --------------------------------------------------------------------------- #
def preflight(target: str, src_dir: Path, cfg: Config, r: Runner) -> None:
    if target == "function":
        return preflight_function(src_dir, cfg, r)
    problems: list[str] = []
    if not (src_dir / "Dockerfile").exists():
        problems.append(f"Dockerfile not found in {src_dir}")
    if shutil.which("docker") is None:
        problems.append("docker CLI not installed")
    elif (
        r.run("docker info", ["docker", "info", "--format", "{{.ServerVersion}}"], check=False).returncode
        != 0
    ):
        problems.append("docker daemon not running (start Docker Desktop)")
    elif (
        cfg.build_secrets
        and r.run("docker buildx", ["docker", "buildx", "version"], check=False).returncode != 0
    ):
        # the legacy builder has no --secret; a Dockerfile patch cannot fix that, so stop here
        problems.append(
            "build secrets need BuildKit: install the docker buildx plugin (docker-buildx-plugin)"
        )

    if target == "local" and cfg.tunnel and shutil.which("cloudflared") is None:
        problems.append("cloudflared not installed (brew install cloudflared) or set DEPLOYER_TUNNEL=0")

    if target == "cloudrun":
        if shutil.which("gcloud") is None:
            problems.append("gcloud CLI not installed (brew install --cask gcloud-cli)")
        else:
            acct = r.run(
                "gcloud auth",
                ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
                check=False,
            )
            if not acct.stdout.strip():
                problems.append("no active gcloud account (gcloud auth login)")
            if not cfg.project:
                problems.append("GCP project not set (GCP_PROJECT_ID or gcloud config set project <ID>)")
            else:
                bill = r.run(
                    "billing",
                    [
                        "gcloud",
                        "billing",
                        "projects",
                        "describe",
                        cfg.project,
                        "--format=value(billingEnabled)",
                    ],
                    check=False,
                )
                if bill.returncode != 0:
                    problems.append(f"cannot read project {cfg.project}: {bill.stderr.strip()[:200]}")
                elif bill.stdout.strip() != "True":
                    problems.append(
                        f"billing not enabled on {cfg.project} (link a billing account in the console)"
                    )
                apis = r.run(
                    "apis",
                    [
                        "gcloud",
                        "services",
                        "list",
                        "--enabled",
                        "--project",
                        cfg.project,
                        "--format=value(config.name)",
                    ],
                    check=False,
                )
                for api in ("run.googleapis.com", "artifactregistry.googleapis.com"):
                    if api not in apis.stdout.split():
                        problems.append(f"API not enabled: {api} (gcloud services enable {api})")
                repo = r.run(
                    "repo",
                    [
                        "gcloud",
                        "artifacts",
                        "repositories",
                        "describe",
                        cfg.repo,
                        "--location",
                        cfg.region,
                        "--project",
                        cfg.project,
                        "--format=value(name)",
                    ],
                    check=False,
                )
                if repo.returncode != 0:
                    problems.append(
                        f"Artifact Registry repo '{cfg.repo}' missing in {cfg.region} (gcloud artifacts repositories "
                        f"create {cfg.repo} --repository-format=docker --location={cfg.region})"
                    )
            host = f"{cfg.region}-docker.pkg.dev"
            try:
                helpers = json.loads((Path.home() / ".docker" / "config.json").read_text()).get(
                    "credHelpers", {}
                )
            except (OSError, ValueError):
                helpers = {}
            if helpers.get(host) != "gcloud":
                problems.append(f"docker not authenticated for {host} (gcloud auth configure-docker {host})")

    if problems:
        raise StepError("preflight", "user_action_required", "\n".join(f"- {p}" for p in problems))


# --------------------------------------------------------------------------- #
# database provisioning ("SQLite -> managed Postgres"): sidecar on local/node, Cloud SQL on cloudrun
# --------------------------------------------------------------------------- #
# Evidence of the engine an app talks to: its driver, a URL scheme, an ORM provider setting.
# (An ORM alone — sqlalchemy, prisma — says nothing about the engine, so it is not evidence.)
_DB_EVIDENCE = {
    "postgres": re.compile(
        r"psycopg|asyncpg|pg8000|\bfrom pg\b|require\(['\"](?:pg|postgres)['\"]\)|from ['\"](?:pg|postgres)['\"]"
        r"|['\"](?:pg|postgres|pg-promise)['\"]\s*:|\bpostgres(?:ql)?(?:\+\w+)?://|\[(?:postgres(?:ql)?|asyncpg)\]"
        r"|provider\s*=\s*['\"]postgres(?:ql)?['\"]|\blib/pq\b|jackc/pgx|gem ['\"]pg['\"]",
        re.IGNORECASE,
    ),
    "mysql": re.compile(
        r"pymysql|mysqlclient|aiomysql|mysql-connector|\bmariadb\b|require\(['\"]mysql2?['\"]\)"
        r"|from ['\"]mysql2?(?:/promise)?['\"]|['\"]mysql2?['\"]\s*:|\bmysql(?:\+\w+)?://|\[(?:mysql|pymysql)\]"
        r"|provider\s*=\s*['\"]mysql['\"]|go-sql-driver/mysql|gem ['\"]mysql2['\"]",
        re.IGNORECASE,
    ),
}
_READS_DB_URL = re.compile(r"\bDATABASE_URL\b")
_SQLITE = re.compile(r"sqlite(?:\+\w+)?:///|sqlite3?\.connect\(", re.IGNORECASE)
# Dependency manifests: when one exists and lists no driver, the app cannot open a server database
_MANIFESTS = {"requirements.txt", "pyproject.toml", "Pipfile", "package.json", "go.mod", "Gemfile"}
_LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock", "Pipfile.lock"}
_SCAN_EXT = {
    ".py",
    ".js",
    ".ts",
    ".mjs",
    ".cjs",
    ".txt",
    ".toml",
    ".json",
    ".env",
    ".example",
    ".prisma",
    ".go",
}
_SCAN_SKIP = {".deployer", "node_modules", ".git", ".venv", "venv", "__pycache__"}


def scan_database(src_dir: Path) -> tuple[str | None, str]:
    """(engine, evidence) of the server database the app talks to: 'postgres', 'mysql' or None.

    A driver or URL scheme decides the engine (Postgres first when both appear). An app that only reads
    DATABASE_URL gets Postgres, unless it falls back to SQLite or its manifest lists no driver: a URL it
    cannot open would just crash it. (An app that hard-codes a SQLite path gets nothing either.)"""
    found: dict[str, str] = {}
    reads_url = sqlite = manifest = None
    for p in sorted(src_dir.rglob("*")):
        if (
            not p.is_file()
            or _SCAN_SKIP.intersection(p.parts)
            or p.name in _LOCKFILES
            or (p.suffix not in _SCAN_EXT and p.name not in _MANIFESTS)
        ):
            continue
        try:
            text = p.read_text(errors="ignore")[:200_000]
        except OSError:
            continue
        rel = p.relative_to(src_dir).as_posix()
        if p.name in _MANIFESTS:
            manifest = manifest or rel
        for engine, rx in _DB_EVIDENCE.items():
            if engine not in found and (m := rx.search(text)):
                found[engine] = f"{rel}: {m.group(0)}"
        if reads_url is None and _READS_DB_URL.search(text):
            reads_url = rel
        if sqlite is None and (m := _SQLITE.search(text)):
            sqlite = f"{rel}: {m.group(0)}"
    for engine in ("postgres", "mysql"):
        if engine in found:
            return engine, found[engine]
    if reads_url is None:
        return None, "no database driver or DATABASE_URL"
    if sqlite:
        return None, f"DATABASE_URL falls back to SQLite ({sqlite})"
    if manifest:
        return None, f"reads DATABASE_URL but {manifest} lists no database driver"
    return "postgres", f"{reads_url}: DATABASE_URL"


def detect_database(src_dir: Path) -> str | None:
    return scan_database(src_dir)[0]


def wants_database(src_dir: Path, cfg: Config) -> str | None:
    if cfg.database == "auto":
        return detect_database(src_dir)
    return cfg.database or None


def database_for(src_dir: Path, cfg: Config, r: Runner) -> str | None:
    """wants_database(), with the reason recorded as a step so the deploy log says why a DB came (or not)."""
    if cfg.database == "auto":
        engine, why = scan_database(src_dir)
    else:
        engine, why = cfg.database or None, f"DEPLOYER_DATABASE={cfg.database or 'none'}"
    r.steps.append(Step("database", f"{engine or 'none'} ({why})", 0, 0.0, "", ""))
    return engine


def db_identifier(app: str, owner: str | None) -> str:
    """Database (and user) name of `app` on a shared server. With `owner` (where the code comes from, see
    main._source_identity) a short hash keeps two sources that share an app name out of each other's data.
    Lowercase [a-z0-9_]; the hashed form fits MySQL's 32-character user names."""
    base = re.sub(r"[^a-z0-9_]", "_", app.lower()) or "app"
    if not owner:
        return base[:40]  # the name deploys used before owners existed
    return f"{base[:23]}_{hashlib.sha256(owner.encode()).hexdigest()[:8]}"


def ensure_local_sidecar(app: str, engine: str, r: Runner, *, scheme: str | None = None) -> str:
    """`engine` sidecar `<app>-<suffix>` on the app's local docker network (nodes.SIDECARS); returns its URL.
    Same script as the node target, run here. Not via Runner: the script carries the password."""
    sc = nodepool.SIDECARS[engine]
    t0 = time.time()
    try:
        cp = subprocess.run(
            ["sh", "-c", nodepool.sidecar_script(sc, app, secrets.token_hex(16))],
            capture_output=True,
            text=True,
            check=False,
            timeout=sc.ready_timeout + 60,
        )
    except subprocess.TimeoutExpired:
        raise StepError(
            "deploy", "timeout", f"{engine} sidecar {app}-{sc.suffix} did not start in time"
        ) from None
    err = cp.stderr.strip()
    r.steps.append(
        Step(
            f"{engine} sidecar",
            f"{app}-{sc.suffix} ({sc.image}, volume {app}-{sc.volume})",
            cp.returncode,
            time.time() - t0,
            "",
            err[-500:] if cp.returncode else "",
        )
    )
    if cp.returncode == 3:
        raise StepError("deploy", "user_action_required", err)
    if cp.returncode != 0:
        raise StepError("deploy", "deploy_error", f"{engine} sidecar {app}-{sc.suffix} failed: {err[-800:]}")
    out = cp.stdout.strip()
    return sc.url.format(
        scheme=scheme or engine, pw=out.splitlines()[-1] if out else "", host=f"{app}-{sc.suffix}"
    )


def ensure_local_postgres(app: str, r: Runner) -> str:
    return ensure_local_sidecar(app, "postgres", r, scheme="postgresql")


_REDIS_URL = re.compile(r"\bREDIS_URL\b")
_MYSQL_SCHEME = re.compile(r"\b(mysql(?:\+\w+)?)://", re.IGNORECASE)


def _code_files(src_dir: Path):
    """(relative path, text) of the files scan_database looks at."""
    for p in sorted(src_dir.rglob("*")):
        if (
            p.is_file()
            and not _SCAN_SKIP.intersection(p.parts)
            and p.name not in _LOCKFILES
            and (p.suffix in _SCAN_EXT or p.name in _MANIFESTS)
        ):
            try:
                yield p.relative_to(src_dir).as_posix(), p.read_text(errors="ignore")[:200_000]
            except OSError:
                continue


def cache_for(src_dir: Path, cfg: Config, r: Runner) -> str | None:
    """'redis' when the app reads REDIS_URL (and DEPLOYER_CACHE is not off); recorded as a step."""
    if not cfg.cache:
        return None
    hit = next((rel for rel, text in _code_files(src_dir) if _REDIS_URL.search(text)), None)
    if hit:
        r.steps.append(Step("cache", f"redis ({hit}: REDIS_URL)", 0, 0.0, "", ""))
    return "redis" if hit else None


def url_scheme(src_dir: Path, engine: str) -> str:
    """URL scheme the app expects. MySQL drivers disagree (SQLAlchemy wants mysql+pymysql://), so a
    scheme already written in the code wins; Postgres keeps postgresql:// (psycopg, pg, prisma all read it)."""
    if engine == "mysql":
        for _, text in _code_files(src_dir):
            if m := _MYSQL_SCHEME.search(text):
                return m.group(1).lower()
    return {"postgres": "postgresql"}.get(engine, engine)


# --------------------------------------------------------------------------- #
# schema migrations: run once with the new image against the app's database, before the app starts
# --------------------------------------------------------------------------- #
def detect_migration(src_dir: Path) -> tuple[list[str], str] | None:
    """(command, evidence) of the app's migration tool. Re-running them is a no-op once applied."""
    if (src_dir / "alembic.ini").is_file():
        return ["alembic", "upgrade", "head"], "alembic.ini"
    manage = src_dir / "manage.py"
    if manage.is_file() and "django.core.management" in manage.read_text(errors="ignore"):
        return ["python", "manage.py", "migrate", "--noinput"], "manage.py (Django)"
    if (src_dir / "prisma" / "schema.prisma").is_file() and (src_dir / "prisma" / "migrations").is_dir():
        return ["npx", "prisma", "migrate", "deploy"], "prisma/migrations"
    return None


def migration_for(src_dir: Path, cfg: Config, r: Runner, *, has_database: bool) -> list[str] | None:
    if not (cfg.migrate and has_database):
        return None
    found = detect_migration(src_dir)
    if found:
        r.steps.append(Step("migration", f"{' '.join(found[0])} ({found[1]})", 0, 0.0, "", ""))
    return found[0] if found else None


def _migration_failed(argv: list[str], output: str, where: str) -> StepError:
    return StepError(
        "deploy",
        "runtime_error",
        f"migration `{' '.join(argv)}` failed {where}; the new version was not started",
        app_logs=output[-TAIL:],
    )


def migrate_local(
    image: str, argv: list[str], env: dict, r: Runner, *, network: str = nodepool.NETWORK
) -> None:
    cmd = ["docker", "run", "--rm", "--network", network]
    for k in env:
        cmd += ["-e", k]
    cmd += ["--entrypoint", argv[0], image, *argv[1:]]  # the image's ENTRYPOINT would wrap the command
    cp = r.run(
        "migrate", cmd, timeout=600, check=False, stage="deploy", env={k: str(v) for k, v in env.items()}
    )
    if cp.returncode != 0:
        raise _migration_failed(argv, cp.stdout + cp.stderr, f"(rc={cp.returncode})")


def migrate_cloudrun(
    service: str,
    image: str,
    argv: list[str],
    cfg: Config,
    r: Runner,
    *,
    conn: str | None,
    db_secret: str | None,
    env: dict,
) -> None:
    """A Cloud Run job `<service>-migrate` with the new image: same database, same identity as the service.
    Runs before the new revision exists, so a failed migration leaves traffic where it was."""
    job = f"{service}-migrate"[:63]
    cmd = [
        "gcloud",
        "run",
        "jobs",
        "deploy",
        job,
        "--image",
        image,
        "--command",
        argv[0],
        "--args",
        ",".join(argv[1:]),
        "--max-retries",
        "0",
        "--task-timeout",
        "600s",
        "--region",
        cfg.region,
        "--project",
        str(cfg.project),
        "--execute-now",
        "--wait",
        "--quiet",
    ]
    if conn:
        cmd += ["--set-cloudsql-instances", conn]
    if account := _runtime_service_account(service, cfg):
        cmd += ["--service-account", account]
    cmd += [f"--set-secrets=DATABASE_URL={db_secret}:latest"] if db_secret else ["--clear-secrets"]
    with env_vars_file(env) as env_file:
        flags = ["--env-vars-file", env_file] if env_file else []
        cp = r.run("migrate (cloud run job)", cmd + flags, timeout=900, check=False, stage="deploy")
    if cp.returncode != 0:
        raise _migration_failed(argv, cp.stderr + cp.stdout, f"in job {job}; traffic untouched")


SIDECAR_TARGETS = {"local", "node"}


def provisioned_env(src_dir: Path, targets, cfg: Config) -> set[str]:
    """Env names the deployer fills in on every one of `targets` when the user leaves them blank:
    DATABASE_URL (Postgres everywhere but function; MySQL on sidecar targets), REDIS_URL (sidecar targets)."""
    targets, r = set(targets), Runner()
    names = set()
    engine = wants_database(src_dir, cfg)
    if engine and "function" not in targets and (engine == "postgres" or targets <= SIDECAR_TARGETS):
        names.add("DATABASE_URL")
    if targets <= SIDECAR_TARGETS and cache_for(src_dir, cfg, r):
        names.add("REDIS_URL")
    return names


def _attach_sidecars(src_dir: Path, cfg: Config, r: Runner, out: Outcome, env: dict, ensure) -> None:
    """DATABASE_URL / REDIS_URL from sidecars (local, node) unless the user gave their own.
    `ensure(engine, scheme) -> url` starts the sidecar where the app runs."""
    name = slug(src_dir.name)
    if "DATABASE_URL" not in env and (engine := database_for(src_dir, cfg, r)):
        env["DATABASE_URL"] = ensure(engine, url_scheme(src_dir, engine))
        out.handles.update(database=engine, db_container=f"{name}-db")
    if "REDIS_URL" not in env and cache_for(src_dir, cfg, r) == "redis":
        env["REDIS_URL"] = ensure("redis", "redis")
        out.handles.update(cache="redis", cache_container=f"{name}-redis")


def _pg_socket_url(serving_url: str | None, name: str, conn: str) -> bool:
    return (
        bool(serving_url)
        and serving_url.startswith(f"postgresql://{name}:")
        and serving_url.endswith(f"@/{name}?host=/cloudsql/{conn}")
    )


def owner_label(cfg: Config) -> str | None:
    return hashlib.sha256(cfg.db_owner.encode()).hexdigest()[:8] if cfg.db_owner else None


_NOT_FOUND = re.compile(r"\b404\b|does not exist|not found", re.IGNORECASE)


def create_cloudsql_instance(cfg: Config, r: Runner) -> None:
    """The shared Postgres instance, smallest tier (ENTERPRISE edition is what allows db-f1-micro)."""
    r.run(
        "cloudsql create instance",
        [
            "gcloud",
            "sql",
            "instances",
            "create",
            cfg.cloudsql_instance,
            "--database-version=POSTGRES_16",
            "--edition=ENTERPRISE",
            f"--tier={cfg.cloudsql_tier}",
            f"--region={cfg.region}",
            "--project",
            str(cfg.project),
            "--quiet",
        ],
        timeout=1800,
        stage="deploy",
    )


def ensure_cloudsql_database(
    app: str, cfg: Config, r: Runner, serving_url: str | None = None, *, adopt_legacy: bool = False
) -> tuple[str, str]:
    """Per-app database + user on the shared Cloud SQL instance. Returns (connection_name, DATABASE_URL).
    `serving_url`: DATABASE_URL of the revision serving traffic; reused so its password stays valid.
    `adopt_legacy`: the live service is this owner's (or predates owners), so a database it already uses
    under the pre-owner name stays its database instead of the app starting over empty."""
    inst, proj = cfg.cloudsql_instance, str(cfg.project)
    describe = [
        "gcloud",
        "sql",
        "instances",
        "describe",
        inst,
        "--project",
        proj,
        "--format=value(state,connectionName)",
    ]
    cp = r.run("cloudsql instance", describe, check=False, stage="deploy")
    missing = cp.returncode != 0 and _NOT_FOUND.search(cp.stderr)
    if missing and cfg.cloudsql_autocreate:
        create_cloudsql_instance(cfg, r)
        cp = r.run("cloudsql instance", describe, check=False, stage="deploy")
    if cp.returncode != 0 or not cp.stdout.strip():
        hint = (
            "create it, set CLOUDSQL_INSTANCE, or CLOUDSQL_AUTOCREATE=1"
            if missing
            else cp.stderr.strip()[-300:]
        )
        raise StepError(
            "deploy", "user_action_required", f"Cloud SQL instance '{inst}' unavailable in {proj}: {hint}"
        )
    state, conn = cp.stdout.split()
    if state != "RUNNABLE":
        raise StepError(
            "deploy", "user_action_required", f"Cloud SQL instance '{inst}' is {state}, not RUNNABLE yet"
        )
    legacy = db_identifier(app, None)
    if adopt_legacy and cfg.db_owner and _pg_socket_url(serving_url, legacy, conn):
        r.steps.append(
            Step("cloudsql reuse", f"keep database {legacy} of the serving revision", 0, 0.0, "", "")
        )
        if read_db_secret(cfg, legacy) != serving_url:
            remember_db_url(cfg, r, legacy, str(serving_url))
        return conn, str(serving_url)
    dbname = user = db_identifier(app, cfg.db_owner)
    dbs = r.run(
        "cloudsql databases",
        ["gcloud", "sql", "databases", "list", "--instance", inst, "--project", proj, "--format=value(name)"],
        stage="deploy",
    )
    if dbname not in dbs.stdout.split():
        r.run(
            "cloudsql create db",
            [
                "gcloud",
                "sql",
                "databases",
                "create",
                dbname,
                "--instance",
                inst,
                "--project",
                proj,
                "--quiet",
            ],
            stage="deploy",
        )
    password = secrets.token_hex(12)
    users = r.run(
        "cloudsql users",
        ["gcloud", "sql", "users", "list", "--instance", inst, "--project", proj, "--format=value(name)"],
        stage="deploy",
    )
    exists = user in users.stdout.split()
    stored = read_db_secret(cfg, dbname)
    # Resetting the password would cut off the live revision (and any rollback to it) before the
    # candidate has passed verify, so keep the one it already uses; Secret Manager holds it when the
    # revision's env cannot tell (mounted secret, or a deploy from another machine).
    if exists and _pg_socket_url(serving_url, dbname, conn):
        if stored != serving_url:
            remember_db_url(cfg, r, dbname, str(serving_url))
        return conn, str(serving_url)
    if exists and _pg_socket_url(stored, dbname, conn):
        r.steps.append(Step("cloudsql reuse", f"password of {user} from Secret Manager", 0, 0.0, "", ""))
        return conn, str(stored)
    verb = "set-password" if exists else "create"
    cp = subprocess.run(
        [
            "gcloud",
            "sql",
            "users",
            verb,
            user,
            "--instance",
            inst,
            "--project",
            proj,
            "--password",
            password,
            "--quiet",
        ],
        capture_output=True,
        text=True,
        check=False,
    )  # not via Runner: keep the password out of logs
    r.steps.append(
        Step(
            f"cloudsql users {verb}",
            f"gcloud sql users {verb} {user} --instance {inst}",
            cp.returncode,
            0.0,
            "",
            cp.stderr[-500:] if cp.returncode else "",
        )
    )
    if cp.returncode != 0:
        raise StepError(
            "deploy", "deploy_error", f"could not create/update Cloud SQL user {user}: {cp.stderr[-500:]}"
        )
    url = f"postgresql://{user}:{password}@/{dbname}?host=/cloudsql/{conn}"
    remember_db_url(cfg, r, dbname, url)
    return conn, url


# DATABASE_URL of a Cloud SQL database, one secret per database: cloudmorph-db-<db name>. Values travel
# through gcloud's stdin/stdout only, never a command line or a Step.
DB_SECRET_PREFIX = "cloudmorph-db-"
_SOCKET_DB = re.compile(r"@/(\w+)\?host=/cloudsql/")


def db_secret_id(dbname: str) -> str:
    return DB_SECRET_PREFIX + dbname


def _gsecrets(cfg: Config, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["gcloud", "secrets", *args, "--project", str(cfg.project), "--quiet"]
    try:
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=False, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def read_db_secret(cfg: Config, dbname: str) -> str | None:
    if not cfg.db_secret:
        return None
    cp = _gsecrets(cfg, "versions", "access", "latest", f"--secret={db_secret_id(dbname)}")
    return (cp.stdout.strip() or None) if cp.returncode == 0 else None


def remember_db_url(cfg: Config, r: Runner, dbname: str, url: str) -> bool:
    """Store `url` as the latest version of the database's secret (created on first use)."""
    if not cfg.db_secret:
        return False
    sid = db_secret_id(dbname)
    cp = _gsecrets(cfg, "versions", "add", sid, "--data-file=-", stdin=url)
    if cp.returncode != 0 and ("NOT_FOUND" in cp.stderr or "not found" in cp.stderr.lower()):
        cp = _gsecrets(cfg, "create", sid, "--replication-policy=automatic", "--data-file=-", stdin=url)
    r.steps.append(
        Step(
            "db secret",
            f"store DATABASE_URL in {sid}",
            cp.returncode,
            0.0,
            "",
            cp.stderr[-300:] if cp.returncode else "",
        )
    )
    return cp.returncode == 0


def _runtime_service_account(service: str, cfg: Config) -> str | None:
    """The identity the service runs as: DEPLOYER_APP_SERVICE_ACCOUNT (what deploy passes), else the
    service's own, else the project's default compute account."""
    if account := cfg.runtime_account():
        return account
    cp = subprocess.run(
        _gcr(cfg, "services", "describe", service, "--format=value(spec.template.spec.serviceAccountName)"),
        capture_output=True,
        text=True,
        check=False,
    )
    if cp.returncode == 0 and cp.stdout.strip():
        return cp.stdout.strip()
    cp = subprocess.run(
        ["gcloud", "projects", "describe", str(cfg.project), "--format=value(projectNumber)"],
        capture_output=True,
        text=True,
        check=False,
    )
    number = cp.stdout.strip() if cp.returncode == 0 else ""
    return f"{number}-compute@developer.gserviceaccount.com" if number.isdigit() else None


def mount_db_secret(service: str, db_url: str, cfg: Config, r: Runner) -> str | None:
    """Secret id to mount as DATABASE_URL, so the password is not readable in the console as a plain env var.
    None (-> plain env var, as before) when the secret does not hold exactly `db_url` or the service
    account cannot be given access to it."""
    m = _SOCKET_DB.search(db_url)
    if not (cfg.db_secret and cfg.db_secret_mount) or not m or read_db_secret(cfg, m.group(1)) != db_url:
        return None
    sid = db_secret_id(m.group(1))
    account = _runtime_service_account(service, cfg)
    cp = (
        _gsecrets(
            cfg,
            "add-iam-policy-binding",
            sid,
            f"--member=serviceAccount:{account}",
            "--role=roles/secretmanager.secretAccessor",
        )
        if account
        else subprocess.CompletedProcess([], 1, "", "runtime service account unknown")
    )
    r.steps.append(
        Step(
            "db secret access",
            f"{account or '?'} -> {sid}",
            cp.returncode,
            0.0,
            "",
            cp.stderr[-300:] if cp.returncode else "",
        )
    )
    return sid if cp.returncode == 0 else None


@contextmanager
def env_vars_file(env: dict):
    """A 0600 temp file for gcloud --env-vars-file (JSON is YAML), so values stay off the command line."""
    if not env:
        yield None
        return
    fd, path = tempfile.mkstemp(prefix="cloudmorph-env-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({k: str(v) for k, v in env.items()}, f)
        yield path
    finally:
        os.unlink(path)


# --------------------------------------------------------------------------- #
# instance size: cpu / memory / instance counts
# --------------------------------------------------------------------------- #
_MEMORY = re.compile(r"^(\d+(?:\.\d+)?)\s*(Mi|Gi|M|G|MB|GB)?$", re.IGNORECASE)
_LABEL_SIZE = re.compile(r"\bcloudmorph\.(cpu|memory)\s*=\s*[\"']?([\w.]+)", re.IGNORECASE)
CLOUDRUN_MAX_MIB = 32 * 1024


def memory_mib(value: str) -> int:
    m = _MEMORY.match(str(value).strip())
    if not m or float(m.group(1)) <= 0:
        raise ValueError(f"memory must look like 512Mi or 1Gi, got {value!r}")
    n = float(m.group(1))
    return int(n * 1024) if (m.group(2) or "Mi").lower().startswith("g") else int(n)


def format_mib(mib: int) -> str:
    return f"{mib // 1024}Gi" if mib % 1024 == 0 else f"{mib}Mi"


def _format_cpu(cpu: float) -> str:
    return str(int(cpu)) if cpu == int(cpu) else str(cpu)


# Dependencies that need more than the platform default (Cloud Run: 1 vCPU / 512Mi) just to start.
# Heaviest first; matched in dependency manifests only.
_SIZE_RULES = (
    (
        re.compile(
            r"\b(torch|tensorflow(?:-cpu)?|transformers|sentence-transformers|onnxruntime|ultralytics|diffusers"
            r"|@tensorflow/tfjs-node|@xenova/transformers)\b",
            re.IGNORECASE,
        ),
        {"cpu": "2", "memory": "4Gi"},
    ),
    (re.compile(r"\b(playwright|puppeteer|selenium)\b", re.IGNORECASE), {"cpu": "1", "memory": "2Gi"}),
    (
        re.compile(
            r"\b(pandas|numpy|scipy|scikit-learn|polars|opencv-python(?:-headless)?)\b", re.IGNORECASE
        ),
        {"memory": "1Gi"},
    ),
)
_SIZE_MANIFESTS = re.compile(r"^(requirements.*\.txt|pyproject\.toml|Pipfile|package\.json)$")
_JVM_MANIFESTS = {"pom.xml", "build.gradle", "build.gradle.kts"}


def detect_size(src_dir: Path) -> tuple[dict[str, str], str]:
    """(size, evidence) the app's dependencies call for; ({}, ...) when the platform default will do."""
    best: tuple[int, dict[str, str], str] | None = None
    for p in sorted(src_dir.rglob("*")):
        if not p.is_file() or _SCAN_SKIP.intersection(p.parts):
            continue
        rel = p.relative_to(src_dir).as_posix()
        if p.name in _JVM_MANIFESTS and (best is None or best[0] > len(_SIZE_RULES)):
            best = (len(_SIZE_RULES), {"memory": "1Gi"}, f"{rel}: JVM")
            continue
        if not _SIZE_MANIFESTS.match(p.name):
            continue
        try:
            text = p.read_text(errors="ignore")[:200_000]
        except OSError:
            continue
        for rank, (rx, size) in enumerate(_SIZE_RULES):
            if (best is None or rank < best[0]) and (m := rx.search(text)):
                best = (rank, size, f"{rel}: {m.group(0)}")
    return (best[1], best[2]) if best else ({}, "")


def dockerfile_size(src_dir: Path) -> dict[str, str]:
    """`LABEL cloudmorph.memory="1Gi" cloudmorph.cpu="2"` in the Dockerfile: the app's own request, and
    where healer raises the memory after an out-of-memory crash (the change shows up in its diff)."""
    try:
        text = (src_dir / "Dockerfile").read_text(errors="ignore")
    except OSError:
        return {}
    found: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip().upper().startswith("LABEL"):
            found |= {k.lower(): v for k, v in _LABEL_SIZE.findall(line)}
    return found


@dataclass
class Size:
    cpu: float | None = None
    memory_mib: int | None = None

    @property
    def memory(self) -> str | None:
        return format_mib(self.memory_mib) if self.memory_mib else None

    def as_dict(self) -> dict[str, str]:
        return {k: v for k, v in (("cpu", self.cpu and _format_cpu(self.cpu)), ("memory", self.memory)) if v}

    def docker_flags(self) -> list[str]:
        """docker run limits (local and node targets)."""
        flags = ["--cpus", _format_cpu(self.cpu)] if self.cpu else []
        return flags + (["--memory", f"{self.memory_mib}m"] if self.memory_mib else [])


def size_for(src_dir: Path, cfg: Config, r: Runner) -> Size:
    """cpu/memory for this deploy: dependencies < Config (env/override) < Dockerfile LABEL; recorded as a step."""
    picked: dict[str, tuple[str, str]] = {}
    detected, evidence = detect_size(src_dir)
    layers = (
        (evidence, detected),
        ("config", {"cpu": cfg.cpu, "memory": cfg.memory}),
        ("Dockerfile", dockerfile_size(src_dir)),
    )
    for origin, values in layers:
        for key in ("cpu", "memory"):
            if values.get(key):
                picked[key] = (str(values[key]), origin)
    size = Size()
    try:
        if "cpu" in picked:
            size.cpu = float(picked["cpu"][0])
            if size.cpu <= 0:
                raise ValueError(f"cpu must be positive, got {picked['cpu'][0]!r}")
        if "memory" in picked:
            size.memory_mib = memory_mib(picked["memory"][0])
    except ValueError as e:
        raise StepError("deploy", "user_action_required", f"invalid instance size: {e}") from None
    shown = [
        f"cpu={_format_cpu(size.cpu)} ({picked['cpu'][1]})" if size.cpu else "",
        f"memory={size.memory} ({picked['memory'][1]})" if size.memory_mib else "",
    ]
    r.steps.append(Step("instance size", " ".join(filter(None, shown)) or "platform default", 0, 0.0, "", ""))
    return size


def fit_cloudrun(size: Size) -> tuple[Size, list[str]]:
    """Cloud Run only takes some cpu/memory pairs: >4Gi needs 2 vCPU, >8Gi 4, >16Gi 6, >24Gi 8;
    4+ vCPU need 2Gi+, 6+ need 4Gi+; below 1 vCPU allows no concurrency. Round up instead of failing."""
    cpu, mib, notes = size.cpu, size.memory_mib, []
    if mib and mib > CLOUDRUN_MAX_MIB:
        raise StepError(
            "deploy", "user_action_required", f"Cloud Run allows at most 32Gi (asked {format_mib(mib)})"
        )
    if cpu is not None and cpu < 1:
        cpu = 1.0
        notes.append("cpu raised to 1 (Cloud Run needs 1 vCPU to serve concurrent requests)")
    if mib:
        need = next(
            c
            for limit, c in ((4096, 1), (8192, 2), (16384, 4), (24576, 6), (CLOUDRUN_MAX_MIB, 8))
            if mib <= limit
        )
        if (cpu or 1) < need:
            cpu = float(need)
            notes.append(f"cpu raised to {need} (Cloud Run needs it for {format_mib(mib)})")
    if cpu and cpu > 8:
        raise StepError("deploy", "user_action_required", f"Cloud Run allows at most 8 vCPU (asked {cpu})")
    floor = 4096 if cpu and cpu >= 6 else 2048 if cpu and cpu >= 4 else 0
    if floor and (mib or 512) < floor:
        mib = floor
        notes.append(f"memory raised to {format_mib(floor)} (Cloud Run needs it for {_format_cpu(cpu)} vCPU)")
    return Size(cpu, mib), notes


def scaling_flags(cfg: Config, *, database: bool = False) -> list[str]:
    """--min/--max-instances, --concurrency, --timeout for cloudrun and function.
    With a Cloud SQL database the instance count is capped: every instance holds its own connections and a
    small instance (db-f1-micro: 25 connections) runs out long before Cloud Run's default of 100 instances."""
    max_instances = (
        cfg.max_instances if cfg.max_instances is not None else (cfg.db_max_instances if database else None)
    )
    flags = []
    for flag, value in (
        ("--min-instances", cfg.min_instances),
        ("--max-instances", max_instances),
        ("--concurrency", cfg.concurrency),
        ("--timeout", cfg.request_timeout),
    ):
        if value is not None:
            flags += [flag, f"{value}s" if flag == "--timeout" else str(value)]
    return flags


def oom_note(killed: bool, size: Size) -> str:
    """Wording healer recognises as an out-of-memory crash (it raises LABEL cloudmorph.memory)."""
    if not killed:
        return ""
    limit = f" of {size.memory}" if size.memory_mib else ""
    return f" (OOMKilled: the container ran out of memory and exceeded its memory limit{limit})"


def container_oom_killed(name: str) -> bool:
    cp = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.OOMKilled}}", name], capture_output=True, text=True, check=False
    )
    return cp.returncode == 0 and cp.stdout.strip() == "true"


# --------------------------------------------------------------------------- #
# build / push
# --------------------------------------------------------------------------- #
def build_image(src_dir: Path, image: str, platform: str | None, cfg: Config, r: Runner) -> None:
    cmd = ["docker", "build", "-t", image]
    if platform:
        cmd += ["--platform", platform]
    for k, v in cfg.build_args.items():
        cmd += ["--build-arg", f"{k}={v}"]
    for k in cfg.build_secrets:
        cmd += ["--secret", f"id={k},env={k}"]
    env = {"DOCKER_BUILDKIT": "1", **cfg.build_secrets} if cfg.build_secrets else None
    r.run(
        "docker build",
        [*cmd, str(src_dir)],
        timeout=cfg.build_timeout,
        stage="build",
        kind="build_error",
        env=env,
    )


def push_image(image: str, cfg: Config, r: Runner) -> None:
    r.run(
        "docker push", ["docker", "push", image], timeout=cfg.build_timeout, stage="push", kind="push_error"
    )


# --------------------------------------------------------------------------- #
# local target
# --------------------------------------------------------------------------- #
def container_state(name: str) -> tuple[str, int]:
    cp = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if cp.returncode != 0:
        return "missing", -1
    status, code = cp.stdout.split()
    return status, int(code)


def container_logs(name: str, tail: int = 200) -> str:
    cp = subprocess.run(
        ["docker", "logs", "--tail", str(tail), name], capture_output=True, text=True, check=False
    )
    return (cp.stdout + cp.stderr)[-TAIL:]


def start_tunnel(local_url: str, log_path: Path, r: Runner) -> tuple[str, int]:
    """Start a Cloudflare quick tunnel and return (public_url, pid) as soon as the URL is printed."""
    t0 = time.time()
    cmd = ["cloudflared", "tunnel", "--no-autoupdate", "--url", local_url]
    with open(log_path, "w") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    url, deadline = None, time.time() + 40
    while time.time() < deadline:
        text = log_path.read_text(errors="replace")
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", text)
        if m:
            url = m.group(0)
            break
        if proc.poll() is not None:
            break
        time.sleep(1)
    text = log_path.read_text(errors="replace")
    r.steps.append(Step("cloudflared tunnel", " ".join(cmd), proc.poll(), time.time() - t0, text[-TAIL:], ""))
    if not url:
        proc.terminate()
        raise StepError(
            "expose", "deploy_error", "cloudflared did not print a trycloudflare URL\n" + text[-TAIL:]
        )
    return url, proc.pid


def tunnel_registered(log_path: Path, wait: int = 30) -> bool:
    """The edge connection is registered a few seconds after the URL is printed; wait for it."""
    deadline = time.time() + wait
    while time.time() < deadline:
        if "Registered tunnel connection" in log_path.read_text(errors="replace"):
            return True
        time.sleep(1)
    return False


# --------------------------------------------------------------------------- #
# dropping a database: dump first, never delete data without a copy
# --------------------------------------------------------------------------- #
BACKUP_DIR = Path(__file__).resolve().parents[1] / ".cloudmorph" / "backups"
# run inside the sidecar (its env has the password); Redis is a cache here, so it is not dumped
_DUMPS = {
    "postgres": "pg_dump -U app app",
    "mysql": 'mysqldump -uapp -p"$MYSQL_PASSWORD" --no-tablespaces app',
}


def sidecars_of(handles: dict) -> list[tuple[str, str, str]]:
    """(engine, container, volume) of the sidecars a deploy attached."""
    found = []
    for key, engine in (
        ("db_container", handles.get("database") or "postgres"),
        ("cache_container", "redis"),
    ):
        if container := handles.get(key):
            sc = nodepool.SIDECARS.get(engine, nodepool.SIDECARS["postgres"])
            found.append((sc.engine, container, f"{container.removesuffix('-' + sc.suffix)}-{sc.volume}"))
    return found


def backup_path(container: str, ext: str = "sql") -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"{container}-{time.strftime('%Y%m%d-%H%M%S')}.{ext}"
    dest.touch(mode=0o600)  # the app's data: readable by this user only
    return dest


def drop_local_sidecars(handles: dict) -> list[str]:
    """Dump each database sidecar to BACKUP_DIR, then remove the container *and* its volume.
    Raises (and keeps everything) when a dump fails, or when a volume outlived its container."""
    done: list[str] = []
    for engine, container, volume in sidecars_of(handles):
        exists = (
            subprocess.run(["docker", "inspect", container], capture_output=True, check=False).returncode == 0
        )
        has_volume = (
            subprocess.run(
                ["docker", "volume", "inspect", volume], capture_output=True, check=False
            ).returncode
            == 0
        )
        if not exists:
            if has_volume:
                raise RuntimeError(
                    f"not dropping {volume}: its container {container} is gone, so it cannot be dumped"
                )
            continue
        if engine in _DUMPS:
            subprocess.run(["docker", "start", container], capture_output=True, check=False)
            dest = backup_path(container)
            with dest.open("wb") as f:
                cp = subprocess.run(
                    ["docker", "exec", container, "sh", "-c", _DUMPS[engine]],
                    stdout=f,
                    stderr=subprocess.PIPE,
                    check=False,
                )
            if cp.returncode != 0:
                dest.unlink(missing_ok=True)
                raise RuntimeError(
                    f"not dropping {container}: backup failed: {cp.stderr.decode(errors='replace')[-300:]}"
                )
            done.append(f"backed up {container} to {dest}")
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True, check=False)
        done.append(f"removed {engine} {container} and volume {volume}")
    return done


def drop_cloudsql_database(handles: dict, cfg: Config) -> list[str]:
    """Export the app's Cloud SQL database to CLOUDSQL_BACKUP_BUCKET, then delete it, its user and secret.
    Without a bucket nothing is deleted (managed data has no other copy CloudMorph could point to)."""
    dbname, conn = handles.get("cloudsql_database"), handles.get("cloudsql_connection")
    if not (dbname and conn):
        return []
    bucket = os.environ.get("CLOUDSQL_BACKUP_BUCKET", "").strip().removeprefix("gs://").rstrip("/")
    if not bucket:
        raise RuntimeError(
            f"not dropping Cloud SQL database {dbname} without a backup: set CLOUDSQL_BACKUP_BUCKET "
            "(a bucket the instance's service account can write to)"
        )
    inst, proj = conn.rsplit(":", 1)[-1], str(cfg.project)
    dest = f"gs://{bucket}/{dbname}-{time.strftime('%Y%m%d-%H%M%S')}.sql"
    steps = [
        (
            ["gcloud", "sql", "export", "sql", inst, dest, f"--database={dbname}"],
            f"backed up {dbname} to {dest}",
        ),
        (
            ["gcloud", "sql", "databases", "delete", dbname, "--instance", inst],
            f"deleted Cloud SQL database {dbname}",
        ),
        (
            ["gcloud", "sql", "users", "delete", dbname, "--instance", inst],
            f"deleted Cloud SQL user {dbname}",
        ),
        (["gcloud", "secrets", "delete", db_secret_id(dbname)], f"deleted secret {db_secret_id(dbname)}"),
    ]
    done: list[str] = []
    for i, (cmd, what) in enumerate(steps):
        cp = subprocess.run(
            [*cmd, "--project", proj, "--quiet"], capture_output=True, text=True, check=False, timeout=900
        )
        if cp.returncode == 0:
            done.append(what)
        elif i == 0 or not _NOT_FOUND.search(
            cp.stderr
        ):  # the export must succeed; later steps may be done already
            raise RuntimeError(f"{' '.join(cmd[:4])} failed: {cp.stderr.strip()[-300:]}")
    return done


def cleanup_local(handles: dict, *, drop_database: bool = False) -> list[str]:
    """Stop the container and tunnel of a previous local deploy (idempotent). Sidecars survive redeploys
    (healer retries must keep the data); drop_database=True dumps and removes them (explicit cleanup)."""
    done: list[str] = []
    pid = handles.get("tunnel_pid")
    if pid:
        try:
            os.kill(int(pid), 15)
            done.append(f"stopped tunnel pid {pid}")
        except ProcessLookupError:
            pass
    cname = handles.get("container_name")
    if cname:
        cp = subprocess.run(["docker", "rm", "-f", cname], capture_output=True, text=True, check=False)
        if cp.returncode == 0:
            done.append(f"removed container {cname}")
    if drop_database:  # after the app is gone, so the dump is consistent
        done += drop_local_sidecars(handles)
    if network := handles.get("network"):  # refused (and kept) while a sidecar is still attached
        subprocess.run(["docker", "network", "rm", network], capture_output=True, check=False)
    return done


def deploy_local(src_dir: Path, cfg: Config, r: Runner, out: Outcome) -> None:
    cleanup_local(_read_handles(src_dir))  # one live local deployment per src_dir (healer redeploys)
    name = slug(src_dir.name)
    tag = cfg.tag or time.strftime("local-%Y%m%d-%H%M%S")
    image, cname = f"{name}:{tag}", f"{name}-{tag}"
    host_port = free_port()
    local_url = f"http://127.0.0.1:{host_port}"
    out.handles.update(image=image, container_name=cname, host_port=host_port, local_url=local_url)

    public_url, tunnel_log = None, src_dir / STATE_DIR / f"tunnel-{tag}.log"
    if cfg.tunnel:
        # Tunnel first: *.trycloudflare.com DNS needs ~1 min to propagate, which now overlaps the build.
        out.stage = "expose"
        public_url, pid = start_tunnel(local_url, tunnel_log, r)
        out.handles.update(tunnel_pid=pid, public_url=public_url)

    try:
        out.stage = "build"
        build_image(src_dir, image, None, cfg, r)

        out.stage = "deploy"
        network = nodepool.app_network(name)  # this app and its sidecars only
        out.handles["network"] = network
        r.run("docker network", ["docker", "network", "create", network], check=False)
        env = dict(cfg.env)
        _attach_sidecars(
            src_dir,
            cfg,
            r,
            out,
            env,
            lambda engine, scheme: ensure_local_sidecar(name, engine, r, scheme=scheme),
        )
        if argv := migration_for(src_dir, cfg, r, has_database="DATABASE_URL" in env):
            migrate_local(image, argv, env, r, network=network)
        run_cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            cname,
            "--network",
            network,
            "-e",
            f"PORT={cfg.container_port}",
        ]
        for k in env:  # name only: docker reads the value from its own env, so it stays off the command line
            run_cmd += ["-e", k]
        size = size_for(src_dir, cfg, r)
        out.handles["size"] = size.as_dict()
        run_cmd += [*size.docker_flags(), "-p", f"{host_port}:{cfg.container_port}", image]
        cp = r.run("docker run", run_cmd, stage="deploy", env={k: str(v) for k, v in env.items()})
        out.handles["container_id"] = cp.stdout.strip()

        out.stage = "verify"
        ok, why = wait_up(
            local_url,
            cfg.health_path,
            cfg.verify_timeout,
            is_dead=lambda: container_state(cname)[0] != "running",
        )
        if not ok:
            status, code = container_state(cname)
            logs = container_logs(cname)
            oom = oom_note(code == 137 and container_oom_killed(cname), size)
            raise StepError(
                "verify",
                "runtime_error",
                f"container status={status} exit_code={code}{oom}; {why}",
                app_logs=logs,
                diagnosis=diagnose_not_reachable(cfg.container_port, why, logs, exited=status != "running"),
            )
        out.url = local_url

        if public_url:
            ok, why = wait_up(public_url, cfg.health_path, 60, interval=3)
            if not ok and not tunnel_registered(tunnel_log):
                raise StepError(
                    "verify", "runtime_error", f"tunnel not registered, public URL unreachable: {why}"
                )
            if not ok:  # registered + local healthy: this machine's resolver has not seen the new name yet
                out.warnings.append(
                    f"public URL not verified from this machine ({why}); local URL {local_url} is healthy"
                )
            out.url = public_url
    except StepError:
        # Logs are already in the StepError; don't leave the container or tunnel of a failed attempt running.
        out.url = None
        cleanup_local(
            {
                "tunnel_pid": out.handles.pop("tunnel_pid", None),
                "container_name": out.handles.pop("container_name", None),
            }
        )
        raise


# --------------------------------------------------------------------------- #
# cloudrun target
# --------------------------------------------------------------------------- #
def _gcr(cfg: Config, *args: str) -> list[str]:
    return ["gcloud", "run", *args, "--region", cfg.region, "--project", str(cfg.project), "--quiet"]


def serving_revision(service: str, cfg: Config, r: Runner) -> str | None:
    """Revision currently serving `service`; None when there is nothing to protect yet (no service, or a
    service whose first deploy never became ready, e.g. healer retrying a broken first deploy).

    Fails closed: a lookup *error* raises instead of returning None, because None means "deploy without
    --no-traffic" and the candidate would serve the public URL before verification.
    `services list --filter` returns rc=0 with empty output for "not found", so no error-text parsing.
    """
    found = r.run(
        "service lookup",
        _gcr(cfg, "services", "list", f"--filter=metadata.name={service}", "--format=value(metadata.name)"),
        stage="deploy",
    )
    if not found.stdout.strip():
        return None
    cp = r.run(
        "serving revision",
        _gcr(cfg, "services", "describe", service, "--format=value(status.traffic[0].revisionName)"),
        stage="deploy",
    )
    return cp.stdout.strip() or None


def latest_revision(service: str, cfg: Config) -> str | None:
    cp = subprocess.run(
        _gcr(
            cfg,
            "revisions",
            "list",
            "--service",
            service,
            "--limit",
            "1",
            "--sort-by",
            "~creationTimestamp",
            "--format=value(name)",
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    return cp.stdout.strip() or None


OWNER_LABEL = "cloudmorph-owner"


def service_owner(service: str, cfg: Config) -> str | None:
    """owner_label() a deploy left on `service`; None for a service deployed before owners existed."""
    cp = subprocess.run(
        _gcr(cfg, "services", "describe", service, f"--format=value(metadata.labels.{OWNER_LABEL})"),
        capture_output=True,
        text=True,
        check=False,
    )
    return (cp.stdout.strip() or None) if cp.returncode == 0 else ""


def _revision_env(revision: str, key: str, cfg: Config) -> str | None:
    """Env var `key` of a Cloud Run revision. Not via Runner: the value may hold a password."""
    cp = subprocess.run(
        _gcr(cfg, "revisions", "describe", revision, "--format=json"),
        capture_output=True,
        text=True,
        check=False,
    )
    if cp.returncode != 0:
        return None
    try:
        containers = json.loads(cp.stdout).get("spec", {}).get("containers", [])
    except ValueError:
        return None
    for container in containers:
        for e in container.get("env", []):
            if e.get("name") == key:
                return e.get("value")
    return None


def cloudrun_logs(service: str, revision: str | None, cfg: Config, r: Runner) -> str:
    flt = f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service}"'
    if revision:
        flt += f' AND resource.labels.revision_name="{revision}"'
    flt += " AND severity>=ERROR"
    for _ in range(3):  # log ingestion lags a few seconds behind the deploy
        cp = r.run(
            "gcloud logging read",
            [
                "gcloud",
                "logging",
                "read",
                flt,
                "--project",
                str(cfg.project),
                "--limit",
                "50",
                "--freshness",
                "20m",
                "--order",
                "asc",
                "--format=value(timestamp,textPayload)",
            ],
            check=False,
        )
        if cp.stdout.strip():
            return cp.stdout[-TAIL:]
        time.sleep(4)
    return "(no ERROR-level logs found for this revision yet)"


def rollback_cloudrun(service: str, revision: str, cfg: Config, r: Runner | None = None) -> bool:
    cmd = _gcr(cfg, "services", "update-traffic", service, f"--to-revisions={revision}=100")
    if r:
        return r.run("rollback", cmd, check=False).returncode == 0
    return subprocess.run(cmd, capture_output=True, text=True, check=False).returncode == 0


def tagged_url(service: str, tag: str, revision: str, cfg: Config, wait: int = 30) -> str | None:
    """URL of `tag` once Cloud Run reports it attached to `revision` (tag URLs are stable strings, but the
    revision behind them switches a few seconds after deploy; checking too early would test the old one)."""
    deadline = time.time() + wait
    while time.time() < deadline:
        cp = subprocess.run(
            _gcr(cfg, "services", "describe", service, "--format=json"),
            capture_output=True,
            text=True,
            check=False,
        )
        if cp.returncode == 0:
            for entry in json.loads(cp.stdout).get("status", {}).get("traffic", []):
                if entry.get("tag") == tag and entry.get("url") and entry.get("revisionName") == revision:
                    return entry["url"]
        time.sleep(2)
    return None


CANDIDATE_TAG = "candidate"


def _cloud_size_flags(
    src_dir: Path, cfg: Config, r: Runner, out: Outcome, *, database: bool = False
) -> list[str]:
    """--cpu/--memory (fitted to what Cloud Run accepts) and the scaling flags; flags left out are inherited
    from the previous revision, so a size set once sticks until it is changed."""
    size, notes = fit_cloudrun(size_for(src_dir, cfg, r))
    for note in notes:
        r.steps.append(Step("instance size", note, 0, 0.0, "", ""))
    out.handles["size"] = size.as_dict()
    if size.cpu and not size.memory_mib:
        size.memory_mib = 512  # functions take --cpu only together with --memory
    flags = ["--cpu", _format_cpu(size.cpu)] if size.cpu else []
    flags += ["--memory", str(size.memory)] if size.memory_mib else []
    return flags + scaling_flags(cfg, database=database)


def deploy_cloudrun(src_dir: Path, cfg: Config, r: Runner, out: Outcome) -> None:
    """Verify a candidate before replacing existing traffic; a first deployment serves immediately."""
    name = slug(src_dir.name)
    service = cfg.service or name
    tag = cfg.tag or time.strftime("v%Y%m%d-%H%M%S")
    image = f"{cfg.region}-docker.pkg.dev/{cfg.project}/{cfg.repo}/{name}:{tag}"
    out.handles.update(image=image, service=service, project=cfg.project, region=cfg.region)

    out.stage = "build"
    build_image(src_dir, image, "linux/amd64", cfg, r)  # Cloud Run runs x86_64 only
    out.stage = "push"
    push_image(image, cfg, r)

    out.stage = "deploy"
    prev = serving_revision(service, cfg, r)  # None -> nothing served yet (first revision takes traffic)
    out.handles["previous_revision"] = prev
    cmd = _gcr(
        cfg, "deploy", service, "--image", image, "--port", str(cfg.container_port), "--tag", CANDIDATE_TAG
    )
    env = dict(cfg.env)
    label = owner_label(cfg)
    db_secret = None
    if "DATABASE_URL" not in env and (engine := database_for(src_dir, cfg, r)):  # the user's own DB wins
        if engine != "postgres":
            raise StepError(
                "deploy",
                "user_action_required",
                f"the app uses {engine}; on cloudrun CloudMorph provisions Cloud SQL for PostgreSQL only. "
                f"Give DATABASE_URL of your {engine} server, or deploy to local/node (a {engine} sidecar)",
            )
        serving_url = _revision_env(prev, "DATABASE_URL", cfg) if prev else None
        mine = bool(prev) and service_owner(service, cfg) in (None, label)
        conn, db_url = ensure_cloudsql_database(name, cfg, r, serving_url, adopt_legacy=mine)
        cmd += ["--add-cloudsql-instances", conn]
        out.handles.update(database="cloudsql", cloudsql_connection=conn)
        if m := _SOCKET_DB.search(db_url):
            out.handles["cloudsql_database"] = m.group(1)
        if db_secret := mount_db_secret(service, db_url, cfg, r):
            out.handles["db_secret"] = db_secret
        else:
            env["DATABASE_URL"] = db_url
    if "REDIS_URL" not in env and cache_for(src_dir, cfg, r):
        out.warnings.append(
            "the app reads REDIS_URL, but cloudrun has no Redis sidecar; give REDIS_URL of a Redis server "
            "(Memorystore, Upstash ...) or deploy to local/node"
        )
    has_db = bool(db_secret) or "DATABASE_URL" in env
    if argv := migration_for(src_dir, cfg, r, has_database=has_db):
        migrate_cloudrun(
            service,
            image,
            argv,
            cfg,
            r,
            conn=out.handles.get("cloudsql_connection"),
            db_secret=db_secret,
            env=env,
        )
    # DATABASE_URL is either a mounted secret or a literal; switching kinds needs the other one cleared
    cmd += [f"--set-secrets=DATABASE_URL={db_secret}:latest"] if db_secret else ["--clear-secrets"]
    if label:
        cmd += ["--update-labels", f"{OWNER_LABEL}={label}"]
    cmd += _cloud_size_flags(src_dir, cfg, r, out, database="cloudsql_connection" in out.handles)
    if prev:
        cmd.append("--no-traffic")
    if cfg.allow_unauthenticated:
        cmd.append("--allow-unauthenticated")
    if sa := cfg.runtime_account():
        cmd += ["--service-account", sa]
    # Probe settings are inherited from the previous revision, so always pass them explicitly.
    if cfg.probe_path:
        cmd.append(
            f"--startup-probe=httpGet.path={cfg.probe_path},httpGet.port={cfg.container_port},"
            "initialDelaySeconds=0,periodSeconds=3,timeoutSeconds=3,failureThreshold=10"
        )
    else:
        cmd.append(f"--startup-probe=tcpSocket.port={cfg.container_port},periodSeconds=3,failureThreshold=10")
    with env_vars_file(env) as env_file:  # every env var is set explicitly: left-over ones would linger
        flags = ["--env-vars-file", env_file] if env_file else ["--clear-env-vars"]
        cp = r.run("gcloud run deploy", cmd + flags, timeout=cfg.deploy_timeout, check=False, stage="deploy")
    if cp.returncode != 0:
        m = re.search(rf"{re.escape(service)}-\d{{5}}-[a-z0-9]{{3}}", cp.stderr + cp.stdout)
        rev = m.group(0) if m else latest_revision(service, cfg)
        out.handles["failed_revision"] = rev
        logs = cloudrun_logs(service, rev, cfg, r)
        diag = None
        if "startup probe" in cp.stderr or "failed to start and listen" in cp.stderr:
            diag = diagnose_not_reachable(cfg.container_port, "startup probe failed", logs)
        raise StepError("deploy", "deploy_error", cp.stderr[-TAIL:], app_logs=logs, diagnosis=diag)
    revision = latest_revision(service, cfg)
    out.handles["revision"] = revision
    if not revision:
        # Without it, tagged_url could accept a stale `candidate` tag and --to-latest would promote
        # a revision nobody verified. Fail closed.
        raise StepError(
            "expose", "deploy_error", f"could not read the revision created by this deploy of {service}"
        )

    out.stage = "expose"
    cp = r.run(
        "service url",
        _gcr(cfg, "services", "describe", service, "--format=value(status.url)"),
        stage="expose",
    )
    url = cp.stdout.strip()
    candidate = tagged_url(service, CANDIDATE_TAG, revision, cfg)
    if candidate is None:
        raise StepError(
            "expose", "deploy_error", f"tag '{CANDIDATE_TAG}' did not attach to revision {revision} in time"
        )
    out.handles["candidate_url"] = candidate

    out.stage = "verify"
    ok, why = wait_up(candidate, cfg.health_path, cfg.verify_timeout)
    if not ok:
        logs = cloudrun_logs(service, revision, cfg, r)
        note = f"; traffic untouched (still on {prev})" if prev else ""
        raise StepError(
            "verify",
            "runtime_error",
            f"candidate revision {revision} failed health check: {why}{note}",
            app_logs=logs,
            diagnosis=diagnose_not_reachable(cfg.container_port, why, logs),
        )
    if prev:
        out.stage = "deploy"
        # Promote exactly the revision that passed verify; --to-latest could pick a newer, unverified one.
        # Every later deploy uses --no-traffic + explicit promotion, so pinning traffic here is safe.
        r.run(
            "promote",
            _gcr(cfg, "services", "update-traffic", service, f"--to-revisions={revision}=100"),
            stage="deploy",
        )
        out.handles["promoted"] = revision
        out.stage = "verify"
        ok, why = wait_up(url, cfg.health_path, 60)
        if not ok:
            logs = cloudrun_logs(service, revision, cfg, r)
            rolled = rollback_cloudrun(service, prev, cfg, r) if cfg.rollback_on_verify_failure else False
            out.handles["rolled_back_to"] = prev if rolled else None
            raise StepError(
                "verify",
                "runtime_error",
                f"service URL unhealthy after promotion: {why}; rolled back to {prev}: {rolled}",
                app_logs=logs,
            )
    out.url = url


# --------------------------------------------------------------------------- #
# node target: least-loaded SSH/Docker host in the pool (GCP / Oracle / AWS / anything)
# --------------------------------------------------------------------------- #
def _pinned_node(reg: nodepool.Registry, app_name: str) -> nodepool.Node | None:
    """The node a stateful app (database sidecar) already lives on, so redeploys stay with their data."""
    from app import fleet

    app = fleet.load_state().apps.get(app_name)
    if app is None or not app.stateful or not app.replicas:
        return None
    try:
        return reg.get(app.replicas[0].node)
    except KeyError:
        return None


def deploy_node(src_dir: Path, cfg: Config, r: Runner, out: Outcome) -> None:
    reg = nodepool.load_registry()
    if not reg.nodes:
        raise StepError(
            "preflight",
            "user_action_required",
            "node pool is empty: create backend/nodes.json (see nodes.example.json)",
        )
    name = slug(src_dir.name)
    tag = cfg.tag or time.strftime("n%Y%m%d-%H%M%S")

    out.stage = "deploy"
    t0 = time.time()
    pinned = _pinned_node(reg, name)
    if pinned is not None:
        # A stateful app's database lives on its node; moving it would silently leave the data behind.
        metrics = [nodepool.probe(pinned)]
        if not metrics[0].ok:
            raise StepError(
                "deploy",
                "user_action_required",
                f"{name} keeps its database on {pinned.name}, which is unreachable ({metrics[0].error}); "
                "not moving it to another node because the data would be left behind",
            )
        node = pinned
        out.handles["pinned"] = True
    else:
        try:
            node, metrics = nodepool.select_node(reg, arch=cfg.node_arch, exclude=set(cfg.node_exclude))
        except RuntimeError as e:
            raise StepError("deploy", "user_action_required", f"node selection failed: {e}") from None
    r.steps.append(
        Step(
            "select node" + (" (pinned: stateful)" if pinned is not None else ""),
            f"ssh probe x{len(metrics)}",
            0,
            time.time() - t0,
            json.dumps(nodepool.metrics_dict(metrics)),
            "",
        )
    )
    image, cname = f"{name}:{tag}", f"{name}-{tag}"
    out.handles.update(
        node=node.name,
        provider=node.provider,
        host=node.host,
        image=image,
        container_name=cname,
        network=nodepool.app_network(name),
        candidates=nodepool.metrics_dict(metrics),
    )

    out.stage = "build"
    build_image(src_dir, image, node.platform, cfg, r)

    out.stage = "push"
    t0 = time.time()
    try:
        secs = nodepool.ship_image(node, image)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        r.steps.append(
            Step(
                "ship image",
                f"docker save {image} | ssh {node.name} docker load",
                1,
                time.time() - t0,
                "",
                str(e),
            )
        )
        raise StepError("push", "push_error", f"could not ship image to {node.name}: {e}") from None
    r.steps.append(Step("ship image", f"docker save {image} | ssh {node.name} docker load", 0, secs, "", ""))

    out.stage = "deploy"
    t0 = time.time()
    try:
        env = dict(cfg.env)
        _attach_sidecars(
            src_dir,
            cfg,
            r,
            out,
            env,
            lambda engine, scheme: nodepool.ensure_sidecar(node, name, engine, scheme=scheme),
        )
        if argv := migration_for(src_dir, cfg, r, has_database="DATABASE_URL" in env):
            rc, output = nodepool.run_once(node, image, argv, env, network=nodepool.app_network(name))
            r.steps.append(Step("migrate (remote)", f"ssh {node.name} {' '.join(argv)}", rc, 0.0, "", ""))
            if rc != 0:
                raise _migration_failed(argv, output, f"on {node.name} (rc={rc})")
        size = size_for(src_dir, cfg, r)
        out.handles["size"] = size.as_dict()
        host_port = nodepool.free_port_on(node)
        cid = nodepool.run_container(
            node,
            image,
            cname,
            host_port,
            cfg.container_port,
            env,
            limits=size.docker_flags(),
            network=nodepool.app_network(name),
        )
    except nodepool.SidecarRefused as e:
        raise StepError("deploy", "user_action_required", str(e)) from None
    except RuntimeError as e:
        r.steps.append(
            Step("docker run (remote)", f"ssh {node.name} docker run ...", 1, time.time() - t0, "", str(e))
        )
        raise StepError("deploy", "deploy_error", str(e)) from None
    r.steps.append(
        Step(
            "docker run (remote)",
            f"ssh {node.name} docker run -p {host_port}:{cfg.container_port} {image}",
            0,
            time.time() - t0,
            cid,
            "",
        )
    )
    node_url = f"http://{node.host}:{host_port}"
    out.handles.update(container_id=cid, host_port=host_port, node_url=node_url)

    out.stage = "verify"
    ok, why = wait_up(
        node_url,
        cfg.health_path,
        cfg.verify_timeout,
        is_dead=lambda: nodepool.container_status(node, cname)[0] != "running",
    )
    if not ok:
        status, code = nodepool.container_status(node, cname)
        logs = nodepool.container_logs(node, cname)
        oom = oom_note(code == 137 and nodepool.container_oom_killed(node, cname), size)
        nodepool.remove_container(node, cname)
        raise StepError(
            "verify",
            "runtime_error",
            f"container on {node.name} status={status} exit_code={code}{oom}; {why}",
            app_logs=logs,
            diagnosis=diagnose_not_reachable(cfg.container_port, why, logs, exited=status != "running"),
        )
    out.url = node_url
    if reg.router is not None:  # publish through the fleet router: stable hostname, scale-out target
        from app import fleet

        out.stage = "expose"
        t0 = time.time()
        try:
            public = fleet.register(
                name,
                image,
                cfg.container_port,
                fleet.Replica(
                    node=node.name, container=cname, host=node.host, port=host_port, provider=node.provider
                ),
                str(src_dir),
                stateful=bool(out.handles.get("database") or out.handles.get("cache")),
                limits=size.docker_flags(),
            )
            r.steps.append(
                Step("router register", f"caddy route {public} -> {node_url}", 0, time.time() - t0, "", "")
            )
            ok, why = wait_up(public, cfg.health_path, 60, interval=3)
            if ok:
                out.handles["public_url"] = public
                out.url = public
            else:
                out.warnings.append(
                    f"router hostname not verified yet ({why}); node URL {node_url} is healthy"
                )
                out.url = public
        except RuntimeError as e:
            r.steps.append(Step("router register", "fleet.register", 1, time.time() - t0, "", str(e)))
            out.warnings.append(f"router registration failed ({e}); serving directly at {node_url}")


# --------------------------------------------------------------------------- #
# function target: Cloud Run functions (gen2). Source-based — Google builds it with buildpacks, so the
# Dockerfile is ignored and the source must be function-shaped (functions-framework + an HTTP entry point).
# --------------------------------------------------------------------------- #
_PY_DECORATED = re.compile(r"@functions_framework\.http\s*\n\s*def\s+(\w+)\s*\(")
_PY_REQUEST_FN = re.compile(r"^def\s+(\w+)\s*\(\s*request\b", re.MULTILINE)
_JS_HTTP = re.compile(r"functions\.http\(\s*['\"]([\w-]+)['\"]")
_FN_NAME = re.compile(r"[^a-z0-9-]+")


def detect_function(src_dir: Path) -> tuple[str, str] | None:
    """Return (runtime, entry_point) when `src_dir` is shaped like a Cloud Run function, else None."""
    main_py = src_dir / "main.py"
    req = src_dir / "requirements.txt"
    if main_py.exists():
        text = main_py.read_text(errors="ignore")
        if m := _PY_DECORATED.search(text):
            return "python312", m.group(1)
        has_ff = req.exists() and "functions-framework" in req.read_text(errors="ignore").lower()
        if has_ff and (m := _PY_REQUEST_FN.search(text)):
            return "python312", m.group(1)
    pkg = src_dir / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
        except ValueError:
            data = {}
        if "@google-cloud/functions-framework" in (data.get("dependencies") or {}):
            index = src_dir / (data.get("main") or "index.js")
            if index.exists() and (m := _JS_HTTP.search(index.read_text(errors="ignore"))):
                return "nodejs20", m.group(1)
    return None


def preflight_function(src_dir: Path, cfg: Config, r: Runner) -> None:
    problems: list[str] = []
    if detect_function(src_dir) is None:
        problems.append(
            "source is not function-shaped: needs functions-framework and an HTTP entry point "
            "(Python: @functions_framework.http def handler(request) in main.py; "
            "Node: functions.http('name', ...) with @google-cloud/functions-framework). See sample-apps/function-hello"
        )
    if shutil.which("gcloud") is None:
        problems.append("gcloud CLI not installed")
    elif not cfg.project:
        problems.append("GCP project not set (GCP_PROJECT_ID or gcloud config set project <ID>)")
    else:
        apis = r.run(
            "apis",
            [
                "gcloud",
                "services",
                "list",
                "--enabled",
                "--project",
                cfg.project,
                "--format=value(config.name)",
            ],
            check=False,
        )
        # cloudresourcemanager: gcloud functions deploy reads the project through it when run as a service account
        for api in (
            "cloudfunctions.googleapis.com",
            "cloudbuild.googleapis.com",
            "run.googleapis.com",
            "cloudresourcemanager.googleapis.com",
        ):
            if api not in apis.stdout.split():
                problems.append(f"API not enabled: {api} (gcloud services enable {api})")
    if problems:
        raise StepError("preflight", "user_action_required", "\n".join(f"- {p}" for p in problems))


def function_logs(name: str, cfg: Config, r: Runner) -> str:
    """Gen2 functions run as a Cloud Run service with the same name; read its ERROR logs."""
    flt = f'resource.type="cloud_run_revision" AND resource.labels.service_name="{name}" AND severity>=ERROR'
    cp = r.run(
        "gcloud logging read",
        [
            "gcloud",
            "logging",
            "read",
            flt,
            "--project",
            str(cfg.project),
            "--limit",
            "40",
            "--freshness",
            "20m",
            "--order",
            "asc",
            "--format=value(timestamp,textPayload)",
        ],
        check=False,
    )
    return cp.stdout[-TAIL:] or "(no ERROR-level runtime logs; see the build log URL in the error above)"


def deploy_function(src_dir: Path, cfg: Config, r: Runner, out: Outcome) -> None:
    detected = detect_function(src_dir)
    if detected is None:  # preflight normally catches this; kept for skip_preflight
        raise StepError(
            "preflight",
            "user_action_required",
            "source is not function-shaped (see sample-apps/function-hello)",
        )
    runtime, entry = detected
    name = _FN_NAME.sub("-", (cfg.service or slug(src_dir.name)).lower()).strip("-")[:62] or "fn"
    if not name[0].isalpha():
        name = f"fn-{name}"[:62]
    out.handles.update(
        function=name, runtime=runtime, entry_point=entry, project=cfg.project, region=cfg.region
    )
    # The source upload must not carry our state or the (ignored) Dockerfile
    (src_dir / ".gcloudignore").write_text(
        ".deployer/\n.gcloudignore\nDockerfile\n__pycache__/\nnode_modules/\n.git/\n"
    )

    out.stage = "deploy"
    cmd = [
        "gcloud",
        "functions",
        "deploy",
        name,
        "--gen2",
        "--runtime",
        runtime,
        "--region",
        cfg.region,
        "--project",
        str(cfg.project),
        "--source",
        str(src_dir),
        "--entry-point",
        entry,
        "--trigger-http",
        "--quiet",
    ]
    if cfg.allow_unauthenticated:
        cmd.append("--allow-unauthenticated")
    if sa := cfg.runtime_account():
        cmd += ["--service-account", sa]
    cmd += _cloud_size_flags(src_dir, cfg, r, out)  # gen2 functions run on Cloud Run: same size rules
    with env_vars_file(cfg.env) as env_file:
        flags = ["--env-vars-file", env_file] if env_file else []
        cp = r.run(
            "gcloud functions deploy", cmd + flags, timeout=cfg.deploy_timeout, check=False, stage="deploy"
        )
    if cp.returncode != 0:
        raise StepError(
            "deploy", "function_error", (cp.stderr or cp.stdout)[-TAIL:], app_logs=function_logs(name, cfg, r)
        )

    out.stage = "expose"
    cp = r.run(
        "function url",
        [
            "gcloud",
            "functions",
            "describe",
            name,
            "--gen2",
            "--region",
            cfg.region,
            "--project",
            str(cfg.project),
            "--format=value(serviceConfig.uri)",
        ],
        stage="expose",
    )
    url = cp.stdout.strip()
    if not url:
        raise StepError("expose", "deploy_error", f"function {name} deployed but has no URL")

    out.stage = "verify"
    ok, why = wait_up(url, cfg.health_path, cfg.verify_timeout)
    if not ok:
        raise StepError(
            "verify",
            "function_error",
            f"function {name} not healthy: {why}",
            app_logs=function_logs(name, cfg, r),
        )
    out.url = url


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def run_pipeline(src_dir: str | os.PathLike, target: str, cfg: Config | None = None) -> Outcome:
    """Full-detail variant of deploy(): stages, handles, warnings. Does not raise."""
    cfg = cfg or Config.from_env()
    if target not in ("local", "cloudrun", "node", "function"):
        raise ValueError("target must be 'local', 'cloudrun', 'node' or 'function'")
    if target in ("cloudrun", "function") and not cfg.project:
        cfg.project = gcloud_default_project()

    pdir = Path(src_dir).resolve()
    (pdir / STATE_DIR).mkdir(parents=True, exist_ok=True)
    r, out, t0 = Runner(), Outcome(target=target), time.time()
    try:
        if not cfg.skip_preflight:
            preflight(target, pdir, cfg, r)
        {
            "local": deploy_local,
            "cloudrun": deploy_cloudrun,
            "node": deploy_node,
            "function": deploy_function,
        }[target](pdir, cfg, r, out)
        out.ok = True
    except StepError as e:
        out.ok, out.stage, out.kind, out.error, out.app_logs, out.diagnosis = (
            False,
            e.stage,
            e.kind,
            e.error,
            e.app_logs,
            e.diagnosis,
        )
    except Exception as e:  # noqa: BLE001 — contract: never raise, report as a failed DeployResult
        out.ok, out.kind, out.error = False, "internal_error", f"{type(e).__name__}: {e}"
    out.steps, out.elapsed = r.steps, time.time() - t0
    _write_state(pdir, out)
    return out


def deploy(src_dir: str, dockerfile: str, target: DeployTarget, **overrides) -> DeployResult:
    """Write `dockerfile` into `src_dir`, build and run on `target`. Never raises on build/run failure.

    `dockerfile` may be "" to reuse the Dockerfile already present in `src_dir`.
    `overrides` are Config fields (e.g. tunnel=False, service="demo", verify_timeout=30).
    """
    cfg = Config.from_env()
    for k, v in overrides.items():
        if not hasattr(cfg, k):
            raise TypeError(f"unknown deployer option: {k}")
        setattr(cfg, k, v)
    if dockerfile:
        Path(src_dir, "Dockerfile").write_text(dockerfile if dockerfile.endswith("\n") else dockerfile + "\n")
    return run_pipeline(src_dir, target, cfg).to_contract()


def cleanup(src_dir: str) -> list[str]:
    """Stop the container/tunnel (and the Postgres sidecar) left by the last local deploy of `src_dir`."""
    return cleanup_local(_read_handles(Path(src_dir).resolve()), drop_database=True)


def last_handles(src_dir: str | os.PathLike) -> dict:
    """Handles (container, tunnel pid, service, node) of the last deploy of `src_dir`, for teardown()."""
    return _read_handles(Path(src_dir).resolve())


def teardown(target: str, handles: dict, *, drop_database: bool = False) -> list[str]:
    """Take down what a successful deploy left serving (idempotent). Databases are kept: data survives a stop.
    drop_database=True also deletes the app's databases, each only after a backup succeeded."""
    if target == "local":
        return cleanup_local(handles, drop_database=drop_database)
    if target == "cloudrun":
        cfg = Config.from_env()
        cfg.project, cfg.region = handles.get("project") or cfg.project, handles.get("region") or cfg.region
        service = handles["service"]
        cp = subprocess.run(
            _gcr(cfg, "services", "delete", service), capture_output=True, text=True, check=False, timeout=300
        )
        if cp.returncode != 0 and not ("could not be found" in cp.stderr or "NOT_FOUND" in cp.stderr):
            raise RuntimeError(f"gcloud run services delete {service} failed: {cp.stderr.strip()[-500:]}")
        done = [f"deleted Cloud Run service {service}"] if cp.returncode == 0 else []
        return done + (drop_cloudsql_database(handles, cfg) if drop_database else [])
    if target == "node":
        from app import fleet

        cname = handles.get("container_name")
        app = next(
            (a for a in fleet.load_state().apps.values() if any(r.container == cname for r in a.replicas)),
            None,
        )
        done: list[str] = []
        if app is not None:  # routed: drop the hostname and every scaled-out replica with it
            fleet.remove(app.name)
            done.append(f"removed {app.name} from the fleet ({len(app.replicas)} replicas)")
        elif cname and nodepool.remove_container(nodepool.load_registry().get(handles["node"]), cname):
            done.append(f"removed container {cname} on {handles['node']}")
        if drop_database and (sidecars := sidecars_of(handles)):
            node = nodepool.load_registry().get(handles["node"])
            for engine, container, volume in sidecars:
                dest = backup_path(f"{handles['node']}-{container}") if engine in _DUMPS else None
                nodepool.drop_sidecar(node, container, volume, _DUMPS.get(engine), dest)
                done += [f"backed up {container} to {dest}"] if dest else []
                done.append(f"removed {engine} {container} and volume {volume} on {handles['node']}")
        if handles.get("network") and handles.get("node"):
            nodepool.remove_network(nodepool.load_registry().get(handles["node"]), handles["network"])
        return done
    raise ValueError(f"unknown target: {target}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main(argv: list[str]) -> int:
    import argparse

    if argv[:1] == ["cleanup"]:
        for line in cleanup(argv[1] if len(argv) > 1 else "."):
            print(line)
        return 0
    ap = argparse.ArgumentParser(
        prog="python -m app.deployer", description="CloudMorph deployer (manual run)"
    )
    ap.add_argument("src_dir")
    ap.add_argument("target", choices=["local", "cloudrun", "node", "function"])
    ap.add_argument("--dockerfile", help="Dockerfile to write into src_dir (default: reuse existing)")
    ap.add_argument("--service")
    ap.add_argument("--tag")
    ap.add_argument("--no-tunnel", action="store_true")
    ap.add_argument("--verify-timeout", type=int)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    opts = {
        k: v for k, v in {"service": a.service, "tag": a.tag, "verify_timeout": a.verify_timeout}.items() if v
    }
    if a.no_tunnel:
        opts["tunnel"] = False
    dockerfile = Path(a.dockerfile).read_text() if a.dockerfile else ""
    res = deploy(a.src_dir, dockerfile, a.target, **opts)  # type: ignore[arg-type]
    if a.json:
        print(res.model_dump_json(indent=2))
    else:
        print(f"success={res.success} exit_code={res.exit_code} url={res.url} duration={res.duration_sec}s")
        if not res.success:
            print(res.stderr)
    return 0 if res.success else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
