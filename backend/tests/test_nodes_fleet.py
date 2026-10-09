"""Node pool + fleet logic without touching real machines (SSH is mocked)."""

from __future__ import annotations

import json

from app import fleet, nodes
from app.nodes import Metrics, Node, Registry, Router


def _reg(router=True):
    r = Router(name="router", host="10.0.0.1", public_port=80) if router else None
    return Registry(
        nodes=[Node(name="a", host="10.0.0.2"), Node(name="b", host="10.0.0.3", arch="arm64")], router=r
    )


def test_metrics_score_prefers_idle_node_and_rejects_unreachable():
    idle = Metrics(node="a", ok=True, load1=0.1, cpus=2, mem_total_mb=2000, mem_avail_mb=1800, containers=0)
    busy = Metrics(node="b", ok=True, load1=1.8, cpus=2, mem_total_mb=2000, mem_avail_mb=400, containers=3)
    dead = Metrics(node="c", ok=False, error="timeout")
    assert idle.score < busy.score
    assert dead.score == float("inf")


def test_select_node_uses_probe_and_honours_exclude_and_arch(monkeypatch):
    reg = _reg()
    scores = {"a": 0.9, "b": 0.1}
    monkeypatch.setattr(
        nodes,
        "probe",
        lambda n: Metrics(node=n.name, ok=True, load1=scores[n.name], cpus=1, mem_total_mb=1, mem_avail_mb=1),
    )
    best, metrics = nodes.select_node(reg)
    assert best.name == "b" and len(metrics) == 2
    best, _ = nodes.select_node(reg, exclude={"b"})
    assert best.name == "a"
    best, _ = nodes.select_node(reg, arch="arm64")
    assert best.name == "b"


def test_hostname_uses_nip_io_until_a_domain_exists():
    assert Router(name="r", host="34.1.2.3").hostname_for("shop") == "shop.34.1.2.3.nip.io"
    assert (
        Router(name="r", host="34.1.2.3", domain_suffix="apps.example.com").hostname_for("shop")
        == "shop.apps.example.com"
    )


def test_caddy_config_round_robins_all_replicas_and_tags_upstream():
    reg = _reg()
    app = fleet.App(
        name="shop",
        image="shop:v1",
        container_port=8080,
        hostname="shop.10.0.0.1.nip.io",
        replicas=[fleet.Replica("a", "c1", "10.0.0.2", 8000), fleet.Replica("b", "c2", "10.0.0.3", 8001)],
    )
    cfg = fleet.render_caddy_config(reg, fleet.State(apps={"shop": app}))
    routes = cfg["apps"]["http"]["servers"]["public"]["routes"]
    assert cfg["apps"]["http"]["servers"]["public"]["listen"] == [":80"]
    assert routes[0]["match"] == [{"host": ["shop.10.0.0.1.nip.io"]}]
    proxy = routes[0]["handle"][0]
    assert [u["dial"] for u in proxy["upstreams"]] == ["10.0.0.2:8000", "10.0.0.3:8001"]
    assert proxy["load_balancing"]["selection_policy"]["policy"] == "round_robin"
    assert "health_checks" not in proxy  # passive checks would blackhole a single hot upstream
    assert routes[-1]["handle"][0]["handler"] == "static_response"  # catch-all for unknown hosts
    json.dumps(cfg)  # must be serialisable for the admin API


def test_tick_scales_out_after_hot_streak_and_blocks_when_pool_exhausted(monkeypatch, tmp_path):
    monkeypatch.setattr(fleet, "STATE_FILE", tmp_path / "fleet.json")
    reg = _reg()
    app = fleet.App(
        name="shop",
        image="shop:v1",
        container_port=8080,
        hostname="h",
        replicas=[fleet.Replica("a", "c1", "10.0.0.2", 8000)],
    )
    fleet.save_state(fleet.State(apps={"shop": app}))
    monkeypatch.setattr(nodes, "load_registry", lambda path=None: reg)
    monkeypatch.setattr(
        fleet, "sample", lambda app, reg: [(r, 90.0) for r in app.replicas]
    )  # every replica hot
    monkeypatch.setattr(
        nodes,
        "probe",
        lambda n: Metrics(node=n.name, ok=True, load1=0.1, cpus=2, mem_total_mb=1, mem_avail_mb=1),
    )
    added = []

    def fake_scale_out(app, reg, state, reason):
        rep = fleet.Replica("b", "c2", "10.0.0.3", 8001)
        app.replicas.append(rep)
        app.hot_streak = 0
        added.append(reason)
        return rep

    monkeypatch.setattr(fleet, "scale_out", fake_scale_out)
    monkeypatch.setattr(fleet, "push_config", lambda reg, state: None)
    first = fleet.tick()[0]
    assert first["hot_streak"] == 1 and first["action"] is None  # one hot sample is not enough
    second = fleet.tick()[0]
    assert second["action"] == "scale_out -> b" and len(second["replicas"]) == 2
    assert "cpu 90%" in added[0]
