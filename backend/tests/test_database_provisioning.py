"""Database detection and provisioning rules (no Docker / gcloud)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import deployer, fleet
from app.deployer import (
    Config,
    cloudsql_conn,
    detect_database,
    fill_store_env,
    render_url,
    sidecar_conn,
    stores_to_provision,
    wants_database,
)
from app.schemas import EnvRequirement


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


@pytest.mark.parametrize("kind", sorted(deployer.SIDECARS))
def test_sidecars_restart_a_stopped_container_instead_of_recreating_it(kind):
    """One script for local and nodes: a stopped sidecar still holds its name (docker run would Conflict)."""
    c = f"todo-{deployer.SIDECARS[kind]['container']}"
    assert f"docker start {c} >/dev/null 2>&1 || docker run -d" in deployer.sidecar_script("todo", kind)
    assert f"-v {c}data:" in deployer.sidecar_script("todo", kind)  # data survives the container


def test_postgres_sidecar_keeps_its_name_and_url():
    assert sidecar_conn("todo", "postgres")["url"] == "postgresql://app:app@todo-db:5432/app"


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


def test_stateful_app_redeploys_to_the_node_that_holds_its_database(monkeypatch, tmp_path):
    """Regression: a push redeploy used to pick the least-loaded node and leave the DB sidecar (data) behind."""
    from app import deployer, nodes
    from app.nodes import Metrics, Node, Registry, Router

    monkeypatch.setattr(fleet, "STATE_FILE", tmp_path / "fleet.json")
    reg = Registry(
        nodes=[Node(name="gcp-1", host="1"), Node(name="aws-1", host="2")], router=Router(name="r", host="3")
    )
    fleet.save_state(
        fleet.State(
            apps={
                "gb-node": fleet.App(
                    name="gb-node",
                    image="i",
                    container_port=8080,
                    hostname="h",
                    stateful=True,
                    replicas=[fleet.Replica("gcp-1", "c", "1", 8000)],
                )
            }
        )
    )
    assert deployer._pinned_node(reg, "gb-node").name == "gcp-1"
    assert deployer._pinned_node(reg, "other") is None
    # a stateless app is not pinned
    fleet.save_state(
        fleet.State(
            apps={
                "web-node": fleet.App(
                    name="web-node",
                    image="i",
                    container_port=8080,
                    hostname="h",
                    replicas=[fleet.Replica("gcp-1", "c", "1", 8000)],
                )
            }
        )
    )
    assert deployer._pinned_node(reg, "web-node") is None
    del nodes, Metrics


def _req(name, role, kind="postgres", template=None):
    return EnvRequirement(
        name=name, scope="runtime", secret=False, resource=kind, role=role, template=template
    )


def test_mysql_drivers_are_not_guessed_as_postgres(tmp_path):
    assert detect_database(_app(tmp_path, **{"requirements.txt": "flask\npymysql"})) is None


def test_the_analyzers_stores_replace_the_guess(tmp_path):
    src = _app(tmp_path, **{"app.py": "import os\nos.environ['DATABASE_URL']"})
    assert stores_to_provision(src, Config(stores=[]), "local") == {}  # analysis found no store
    redis = [_req("REDIS_HOST", "host", "redis")]
    assert stores_to_provision(src, Config(stores=redis), "local") == {"redis": redis}
    assert stores_to_provision(src, Config(stores=redis), "node") == {"redis": redis}
    assert stores_to_provision(src, Config(stores=redis), "cloudrun") == {}  # Cloud Run: Postgres only
    assert stores_to_provision(src, Config(stores=redis, database=None), "local") == {}  # turned off
    guessed = stores_to_provision(src, Config(), "local")  # CLI: no analysis, the old DATABASE_URL guess
    assert [(e.name, e.resource, e.role) for e in guessed["postgres"]] == [
        ("DATABASE_URL", "postgres", "url")
    ]


def test_settings_are_filled_by_role_in_the_apps_own_names():
    mysql = [
        _req("DB_HOST", "host", "mysql"),
        _req("DB_PORT", "port", "mysql"),
        _req("DB_PASS", "password", "mysql"),
        _req("SPRING_DATASOURCE_URL", "url", "mysql", "jdbc:mysql://{host}:{port}/{database}"),
    ]
    assert fill_store_env(mysql, sidecar_conn("shop", "mysql")) == {
        "DB_HOST": "shop-mysql",
        "DB_PORT": "3306",
        "DB_PASS": "app",
        "SPRING_DATASOURCE_URL": "jdbc:mysql://shop-mysql:3306/app",
    }
    s3 = [
        _req("S3_BUCKET", "bucket", "s3"),
        _req("S3_KEY", "access_key", "s3"),
        _req("S3_DB", "database", "s3"),
    ]
    assert fill_store_env(s3, sidecar_conn("shop", "s3")) == {
        "AWS_ENDPOINT_URL_S3": "http://shop-s3:8333",  # SDKs reach MinIO even without an endpoint setting
        "S3_BUCKET": "app",
        "S3_KEY": "app",
    }


def test_cloudsql_socket_urls_follow_the_apps_form():
    conn = cloudsql_conn("p:r:i", "postgresql://u:pw@/d?host=/cloudsql/p:r:i")
    assert (conn["host"], conn["user"], conn["password"], conn["database"]) == (
        "/cloudsql/p:r:i",
        "u",
        "pw",
        "d",
    )
    asyncpg = "postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"
    assert render_url(asyncpg, conn) == "postgresql+asyncpg://u:pw@/d?host=/cloudsql/p:r:i"
    assert render_url("jdbc:postgresql://{host}:{port}/{database}", conn) is None  # JDBC can't use the socket
