"""Deployer (owner: 전동훈).

Build the user's app into a container and run it on `local` (Docker + Cloudflare quick tunnel)
or `cloudrun` (Artifact Registry + Cloud Run). Contract: app/schemas.py::DeployResult, docs/interfaces.md.

    deploy(src_dir, dockerfile, target) -> DeployResult   # never raises on build/run failure

Pipeline (each stage is recorded; the first failing stage decides `stderr`):

    preflight -> build -> [push] -> deploy -> expose -> verify
    cloudrun is blue/green: the new revision is deployed with --no-traffic and a `candidate` tag URL,
    verified there, and only then promoted (--to-latest). A broken revision never serves the public URL.

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

CLI (manual testing):
    uv run python -m app.deployer <src_dir> local|cloudrun [--dockerfile PATH] [--no-tunnel] [--service NAME]
    uv run python -m app.deployer cleanup <src_dir>
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
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
            cloudsql_instance=env.get("CLOUDSQL_INSTANCE", "cloudmorph-pg"),
        )


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
    """App is 'up' when health_path (or "/" as fallback) answers with a status < 500."""
    deadline = time.time() + timeout
    last = "not attempted"
    while time.time() < deadline:
        for path in (health_path, "/") if health_path != "/" else ("/",):
            status, last = http_status(base_url + path)
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
_DB_HINTS = re.compile(
    r"DATABASE_URL|psycopg|asyncpg|sqlalchemy|pg8000|\bfrom pg\b|require\(['\"]pg['\"]\)|['\"]pg['\"]\s*:|prisma|pymysql|mysqlclient",
    re.IGNORECASE,
)
_SCAN_EXT = {".py", ".js", ".ts", ".mjs", ".cjs", ".txt", ".toml", ".json", ".env", ".example"}


def detect_database(src_dir: Path) -> str | None:
    """'postgres' when the app reads DATABASE_URL or ships a Postgres driver; else None.
    (An app that hard-codes a SQLite path gets nothing: injecting a URL it never reads would be theatre.)"""
    for p in src_dir.rglob("*"):
        if (
            p.is_file()
            and p.suffix in _SCAN_EXT
            and ".deployer" not in p.parts
            and "node_modules" not in p.parts
        ):
            try:
                if _DB_HINTS.search(p.read_text(errors="ignore")[:200_000]):
                    return "postgres"
            except OSError:
                continue
    return None


def wants_database(src_dir: Path, cfg: Config) -> str | None:
    if cfg.database == "auto":
        return detect_database(src_dir)
    return cfg.database or None


def ensure_local_postgres(app: str, r: Runner) -> str:
    """Postgres sidecar `<app>-db` on the local docker network; data in volume `<app>-dbdata`."""
    db, net = f"{app}-db", nodepool.NETWORK
    r.run("docker network", ["docker", "network", "create", net], check=False)
    exists = subprocess.run(
        ["docker", "ps", "-q", "-f", f"name=^{db}$"], capture_output=True, text=True, check=False
    )
    if not exists.stdout.strip():
        r.run(
            "docker run (postgres)",
            [
                "docker",
                "run",
                "-d",
                "--restart",
                "unless-stopped",
                "--name",
                db,
                "--network",
                net,
                "-e",
                "POSTGRES_USER=app",
                "-e",
                "POSTGRES_PASSWORD=app",
                "-e",
                "POSTGRES_DB=app",
                "-v",
                f"{app}-dbdata:/var/lib/postgresql/data",
                "postgres:16-alpine",
            ],
            stage="deploy",
        )
    deadline = time.time() + 90
    while time.time() < deadline:
        if (
            subprocess.run(
                ["docker", "exec", db, "pg_isready", "-U", "app", "-q"], capture_output=True, check=False
            ).returncode
            == 0
        ):
            return f"postgresql://app:app@{db}:5432/app"
        time.sleep(1)
    raise StepError("deploy", "deploy_error", f"postgres sidecar {db} did not become ready")


def ensure_cloudsql_database(app: str, cfg: Config, r: Runner) -> tuple[str, str]:
    """Per-app database + user on the shared Cloud SQL instance. Returns (connection_name, DATABASE_URL)."""
    inst, proj = cfg.cloudsql_instance, str(cfg.project)
    cp = r.run(
        "cloudsql instance",
        [
            "gcloud",
            "sql",
            "instances",
            "describe",
            inst,
            "--project",
            proj,
            "--format=value(state,connectionName)",
        ],
        check=False,
        stage="deploy",
    )
    if cp.returncode != 0 or not cp.stdout.strip():
        raise StepError(
            "deploy",
            "user_action_required",
            f"Cloud SQL instance '{inst}' not found in {proj} (create it or set CLOUDSQL_INSTANCE)",
        )
    state, conn = cp.stdout.split()
    if state != "RUNNABLE":
        raise StepError(
            "deploy", "user_action_required", f"Cloud SQL instance '{inst}' is {state}, not RUNNABLE yet"
        )
    dbname = user = re.sub(r"[^a-z0-9_]", "_", app.lower())[:40] or "app"
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
    verb = "set-password" if user in users.stdout.split() else "create"
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
    return conn, f"postgresql://{user}:{password}@/{dbname}?host=/cloudsql/{conn}"


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


def cleanup_local(handles: dict, *, drop_database: bool = False) -> list[str]:
    """Stop the container and tunnel of a previous local deploy (idempotent). The Postgres sidecar survives
    redeploys (healer retries must keep the data); pass drop_database=True from an explicit cleanup."""
    done: list[str] = []
    if drop_database and (db := handles.get("db_container")):
        cp = subprocess.run(["docker", "rm", "-f", db], capture_output=True, text=True, check=False)
        if cp.returncode == 0:
            done.append(f"removed database {db}")
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
        r.run("docker network", ["docker", "network", "create", nodepool.NETWORK], check=False)
        env = dict(cfg.env)
        if "DATABASE_URL" not in env and wants_database(src_dir, cfg) == "postgres":  # the user's own DB wins
            env["DATABASE_URL"] = ensure_local_postgres(name, r)
            out.handles.update(database="postgres", db_container=f"{name}-db")
        run_cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            cname,
            "--network",
            nodepool.NETWORK,
            "-e",
            f"PORT={cfg.container_port}",
        ]
        for k, v in env.items():
            run_cmd += ["-e", f"{k}={v}"]
        run_cmd += ["-p", f"{host_port}:{cfg.container_port}", image]
        cp = r.run("docker run", run_cmd, stage="deploy")
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
            raise StepError(
                "verify",
                "runtime_error",
                f"container status={status} exit_code={code}; {why}",
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


def deploy_cloudrun(src_dir: Path, cfg: Config, r: Runner, out: Outcome) -> None:
    """Blue/green: new revision gets 0% traffic and a tag URL; traffic moves only after it passes verify."""
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
    if "DATABASE_URL" not in env and wants_database(src_dir, cfg) == "postgres":  # the user's own DB wins
        conn, db_url = ensure_cloudsql_database(name, cfg, r)
        env["DATABASE_URL"] = db_url
        cmd += ["--add-cloudsql-instances", conn]
        out.handles.update(database="cloudsql", cloudsql_connection=conn)
    if env:
        cmd += [
            "--set-env-vars",
            "^|^" + "|".join(f"{k}={v}" for k, v in env.items()),
        ]  # '|' delimiter: URLs contain ','-free but '='-rich values
    if prev:
        cmd.append("--no-traffic")
    if cfg.allow_unauthenticated:
        cmd.append("--allow-unauthenticated")
    # Probe settings are inherited from the previous revision, so always pass them explicitly.
    if cfg.probe_path:
        cmd.append(
            f"--startup-probe=httpGet.path={cfg.probe_path},httpGet.port={cfg.container_port},"
            "initialDelaySeconds=0,periodSeconds=3,timeoutSeconds=3,failureThreshold=10"
        )
    else:
        cmd.append(f"--startup-probe=tcpSocket.port={cfg.container_port},periodSeconds=3,failureThreshold=10")
    cp = r.run("gcloud run deploy", cmd, timeout=cfg.deploy_timeout, check=False, stage="deploy")
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
    try:
        node, metrics = nodepool.select_node(reg, arch=cfg.node_arch, exclude=set(cfg.node_exclude))
    except RuntimeError as e:
        raise StepError("deploy", "user_action_required", f"node selection failed: {e}") from None
    r.steps.append(
        Step(
            "select node",
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
        if "DATABASE_URL" not in env and wants_database(src_dir, cfg) == "postgres":  # the user's own DB wins
            env["DATABASE_URL"] = nodepool.ensure_postgres(node, name)
            out.handles.update(database="postgres", db_container=f"{name}-db")
        host_port = nodepool.free_port_on(node)
        cid = nodepool.run_container(node, image, cname, host_port, cfg.container_port, env)
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
        nodepool.remove_container(node, cname)
        raise StepError(
            "verify",
            "runtime_error",
            f"container on {node.name} status={status} exit_code={code}; {why}",
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
                fleet.Replica(node=node.name, container=cname, host=node.host, port=host_port),
                str(src_dir),
                stateful=out.handles.get("database") is not None,
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
# Public API
# --------------------------------------------------------------------------- #
def run_pipeline(src_dir: str | os.PathLike, target: str, cfg: Config | None = None) -> Outcome:
    """Full-detail variant of deploy(): stages, handles, warnings. Does not raise."""
    cfg = cfg or Config.from_env()
    if target not in ("local", "cloudrun", "node"):
        raise ValueError("target must be 'local', 'cloudrun' or 'node'")
    if target == "cloudrun" and not cfg.project:
        cfg.project = gcloud_default_project()

    pdir = Path(src_dir).resolve()
    (pdir / STATE_DIR).mkdir(parents=True, exist_ok=True)
    r, out, t0 = Runner(), Outcome(target=target), time.time()
    try:
        if not cfg.skip_preflight:
            preflight(target, pdir, cfg, r)
        {"local": deploy_local, "cloudrun": deploy_cloudrun, "node": deploy_node}[target](pdir, cfg, r, out)
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
    ap.add_argument("target", choices=["local", "cloudrun", "node"])
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
