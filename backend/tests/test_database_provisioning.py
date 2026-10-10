"""Database detection and provisioning rules (no Docker / gcloud)."""

from __future__ import annotations

from pathlib import Path

from app import deployer, fleet
from app.deployer import Config, detect_database, wants_database


def _app(tmp_path: Path, **files: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    return tmp_path


def test_detects_apps_that_read_database_url_or_ship_a_driver(tmp_path):
    assert (
        detect_database(_app(tmp_path / "a", **{"app.py": "import os\nurl = os.environ['DATABASE_URL']"}))
        == "postgres"
    )
    assert (
        detect_database(_app(tmp_path / "b", **{"requirements.txt": "flask\npsycopg[binary]==3.2"}))
        == "postgres"
    )
    assert (
        detect_database(_app(tmp_path / "c", **{"package.json": '{"dependencies": {"pg": "^8"}}'}))
        == "postgres"
    )


def test_sqlite_only_or_stateless_apps_get_no_database(tmp_path):
    assert (
        detect_database(_app(tmp_path / "d", **{"app.py": "import sqlite3\nconn = sqlite3.connect('x.db')"}))
        is None
    )
    assert detect_database(_app(tmp_path / "e", **{"app.py": "from flask import Flask"})) is None


def test_wants_database_respects_explicit_config(tmp_path):
    src = _app(tmp_path, **{"app.py": "from flask import Flask"})
    assert wants_database(src, Config(database="auto")) is None
    assert wants_database(src, Config(database="postgres")) == "postgres"
    assert wants_database(_app(tmp_path / "f", **{"app.py": "DATABASE_URL"}), Config(database=None)) is None


def test_cloudsql_provisioning_builds_unix_socket_url(monkeypatch):
    calls: list[list[str]] = []

    class R:
        def __init__(self):
            self.steps = []

        def run(self, name, cmd, **kw):
            calls.append(cmd)

            class CP:
                returncode = 0
                stdout = {
                    "cloudsql instance": "RUNNABLE p:r:inst\n",
                    "cloudsql databases": "postgres\n",
                    "cloudsql users": "postgres\n",
                }.get(name, "")
                stderr = ""

            return CP()

    monkeypatch.setattr(
        deployer.subprocess,
        "run",
        lambda *a, **k: type("CP", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    conn, url = deployer.ensure_cloudsql_database("My App", Config(project="p"), R())
    assert conn == "p:r:inst"
    assert url.startswith("postgresql://my_app:") and url.endswith("@/my_app?host=/cloudsql/p:r:inst")
    assert any(
        c[:4] == ["gcloud", "sql", "databases", "create"] for c in calls
    )  # db did not exist -> created


def test_cloudsql_keeps_the_password_the_serving_revision_uses(monkeypatch):
    """Regression: every deploy reset the password before the candidate passed verify, so the revision
    still serving traffic (and any rollback target) lost its database connection."""

    class R:
        def __init__(self):
            self.steps = []

        def run(self, name, cmd, **kw):
            class CP:
                returncode = 0
                stdout = {
                    "cloudsql instance": "RUNNABLE p:r:inst\n",
                    "cloudsql databases": "my_app\n",
                    "cloudsql users": "my_app\n",
                }.get(name, "")
                stderr = ""

            return CP()

    direct: list[list[str]] = []
    monkeypatch.setattr(
        deployer.subprocess,
        "run",
        lambda cmd, **k: (
            direct.append(cmd) or type("CP", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        ),
    )
    serving = "postgresql://my_app:oldpw@/my_app?host=/cloudsql/p:r:inst"
    assert deployer.ensure_cloudsql_database("My App", Config(project="p"), R(), serving) == (
        "p:r:inst",
        serving,
    )
    assert not direct  # no set-password

    # a URL for another database/instance is not reused
    _, url = deployer.ensure_cloudsql_database(
        "My App", Config(project="p"), R(), "postgresql://my_app:x@/my_app?host=/cloudsql/p:r:other"
    )
    assert "x@" not in url and direct[0][:4] == ["gcloud", "sql", "users", "set-password"]


def test_revision_env_reads_database_url_without_runner(monkeypatch):
    out = '{"spec": {"containers": [{"env": [{"name": "A", "value": "1"}, {"name": "DATABASE_URL", "value": "u"}]}]}}'
    monkeypatch.setattr(
        deployer.subprocess, "run", lambda cmd, **k: type("CP", (), {"returncode": 0, "stdout": out})()
    )
    assert deployer._revision_env("svc-00002-abc", "DATABASE_URL", Config(project="p")) == "u"
    assert deployer._revision_env("svc-00002-abc", "MISSING", Config(project="p")) is None


def test_stateful_apps_never_scale_out(monkeypatch, tmp_path):
    monkeypatch.setattr(fleet, "STATE_FILE", tmp_path / "fleet.json")
    from app import nodes
    from app.nodes import Metrics, Node, Registry, Router

    reg = Registry(
        nodes=[Node(name="a", host="1"), Node(name="b", host="2")], router=Router(name="r", host="3")
    )
    app = fleet.App(
        name="gb",
        image="gb:1",
        container_port=8080,
        hostname="h",
        stateful=True,
        replicas=[fleet.Replica("a", "c", "1", 8000)],
    )
    fleet.save_state(fleet.State(apps={"gb": app}))
    monkeypatch.setattr(nodes, "load_registry", lambda path=None: reg)
    monkeypatch.setattr(fleet, "sample", lambda app, reg: [(r, 95.0) for r in app.replicas])
    monkeypatch.setattr(
        nodes,
        "probe",
        lambda n: Metrics(node=n.name, ok=True, load1=0, cpus=2, mem_total_mb=1, mem_avail_mb=1),
    )
    monkeypatch.setattr(
        fleet, "scale_out", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not scale"))
    )
    for _ in range(3):
        row = fleet.tick()[0]
    assert "stateful" in (row["action"] or "") and len(row["replicas"]) == 1
