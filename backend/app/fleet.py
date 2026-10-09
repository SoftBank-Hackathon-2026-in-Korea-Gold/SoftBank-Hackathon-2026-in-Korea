"""Fleet: one public entry point in front of the node pool, and automatic scale-out when a replica is hot.

    user ──▶ router VM (Caddy :80) ──▶ app replica(s) on node(s)      hostname: <app>.<router-ip>.nip.io
                                                                       (or <app>.<domain_suffix> once a domain exists)
    watcher: every N seconds read CPU% of each replica over SSH; above the threshold twice in a row
             -> deploy the same image to the least-loaded *other* node, add it as an upstream, reload Caddy.

State lives in backend/.fleet/fleet.json (apps -> replicas). Caddy is driven through its admin API on the
router VM (localhost:2019 there, reached over SSH), so route changes are atomic and zero-downtime.

CLI:
    python -m app.fleet init-router            start/refresh Caddy on the router VM
    python -m app.fleet status                 apps, replicas, node metrics
    python -m app.fleet watch [--interval 10]  monitor + auto scale-out (blocking)
    python -m app.fleet scale <app> <n>        manual scale to n replicas
    python -m app.fleet remove <app>
    python -m app.fleet stress <app> [--seconds 60] [--concurrency 32]   demo load generator
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app import nodes as nodepool
from app.nodes import Registry

log = logging.getLogger("fleet")
STATE_FILE = Path(__file__).resolve().parents[1] / ".fleet" / "fleet.json"
CADDY_IMAGE = "caddy:2-alpine"
CPU_HOT = float(os.environ.get("FLEET_CPU_HOT", "50"))  # average % of one core, per replica, over an interval
NODE_LOAD_HOT = float(os.environ.get("FLEET_NODE_LOAD_HOT", "0.8"))  # 1-min load average per core
CPU_COLD = float(os.environ.get("FLEET_CPU_COLD", "10"))
MAX_REPLICAS = int(os.environ.get("FLEET_MAX_REPLICAS", "3"))
HOT_STREAK = 2  # consecutive hot samples before scaling out


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
@dataclass
class Replica:
    node: str
    container: str
    host: str
    port: int
    created_at: float = field(default_factory=time.time)
    last_cpu: float | None = None  # average CPU% (of one core) over the last sampling interval
    last_usage_usec: int | None = None
    last_sample_ts: float | None = None

    @property
    def upstream(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class App:
    name: str
    image: str
    container_port: int
    hostname: str
    src_dir: str | None = None
    replicas: list[Replica] = field(default_factory=list)
    hot_streak: int = 0
    stateful: bool = False  # has a database sidecar on its node -> never replicate (data would diverge)
    events: list[dict] = field(default_factory=list)  # scale history shown in the dashboard


@dataclass
class State:
    apps: dict[str, App] = field(default_factory=dict)


def load_state() -> State:
    if not STATE_FILE.exists():
        return State()
    raw = json.loads(STATE_FILE.read_text())
    apps = {}
    for name, a in raw.get("apps", {}).items():
        reps = [Replica(**r) for r in a.pop("replicas", [])]
        apps[name] = App(**a, replicas=reps)
    return State(apps=apps)


def save_state(state: State) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# Router (Caddy on the router VM, configured through its admin API over SSH)
# --------------------------------------------------------------------------- #
INIT_CONFIG = {"admin": {"listen": "0.0.0.0:2019"}, "apps": {"http": {"servers": {}}}}


def init_router(reg: Registry) -> str:
    """Start Caddy on the router VM. Ports are *published* (-p) rather than host-networked because
    Container-Optimized OS drops inbound traffic on the host by default; Docker's port mapping bypasses that.
    The admin API is published on the VM's loopback only and driven over SSH."""
    rt = reg.router
    if rt is None:
        raise RuntimeError("nodes.json has no router")
    cp = nodepool.ssh(
        rt, "mkdir -p ~/caddy && cat > ~/caddy/init.json", timeout=20, stdin=json.dumps(INIT_CONFIG).encode()
    )
    if cp.returncode != 0:
        raise RuntimeError("could not write init config: " + cp.stderr.decode(errors="replace"))
    cmd = (
        f"docker ps -q -f name=^cloudmorph-router$ | grep -q . || "
        f"docker run -d --restart unless-stopped --name cloudmorph-router "
        f"-p {rt.public_port}:{rt.public_port} -p 127.0.0.1:2019:2019 "
        f"-v $HOME/caddy:/etc/caddy -v caddy_data:/data -v caddy_config:/config "
        f"{CADDY_IMAGE} caddy run --config /etc/caddy/init.json"
    )
    nodepool.ssh_text(rt, cmd, timeout=180)
    for _ in range(30):
        if nodepool.ssh(rt, "curl -sf localhost:2019/config/ >/dev/null", timeout=15).returncode == 0:
            break
        time.sleep(1)
    push_config(reg, load_state())
    return f"http://{rt.host}:{rt.public_port}"


def render_caddy_config(reg: Registry, state: State) -> dict:
    rt = reg.router
    routes = []
    for app in state.apps.values():
        if not app.replicas:
            continue
        routes.append(
            {
                "match": [{"host": [app.hostname]}],
                "handle": [
                    {
                        "handler": "reverse_proxy",
                        "upstreams": [{"dial": r.upstream} for r in app.replicas],
                        "load_balancing": {
                            "selection_policy": {"policy": "round_robin"},
                            "try_duration": "5s",
                        },
                        "headers": {
                            "response": {
                                "set": {"X-Cloudmorph-Upstream": ["{http.reverse_proxy.upstream.hostport}"]}
                            }
                        },
                    }
                ],
                "terminal": True,
            }
        )
    routes.append(
        {
            "handle": [
                {
                    "handler": "static_response",
                    "status_code": 200,
                    "headers": {"Content-Type": ["application/json"]},
                    "body": json.dumps(
                        {"router": "cloudmorph", "apps": [a.hostname for a in state.apps.values()]}
                    ),
                }
            ]
        }
    )
    return {
        "admin": {"listen": "0.0.0.0:2019"},
        "apps": {"http": {"servers": {"public": {"listen": [f":{rt.public_port}"], "routes": routes}}}},
    }


def push_config(reg: Registry, state: State) -> None:
    rt = reg.router
    payload = json.dumps(render_caddy_config(reg, state)).encode()
    cp = nodepool.ssh(
        rt,
        "curl -sS -X POST -H 'Content-Type: application/json' localhost:2019/load --data-binary @-",
        timeout=30,
        stdin=payload,
    )
    if cp.returncode != 0:
        raise RuntimeError("caddy reload failed: " + cp.stderr.decode(errors="replace")[-500:])


# --------------------------------------------------------------------------- #
# Registration (called by deployer.deploy_node) and scaling
# --------------------------------------------------------------------------- #
def register(
    app_name: str,
    image: str,
    container_port: int,
    replica: Replica,
    src_dir: str | None = None,
    *,
    stateful: bool = False,
) -> str:
    """Add/replace the primary replica of an app and publish it through the router. Returns the public URL."""
    reg = nodepool.load_registry()
    if reg.router is None:
        raise RuntimeError("no router configured")
    state = load_state()
    app = state.apps.get(app_name)
    if app is None:
        app = App(
            name=app_name,
            image=image,
            container_port=container_port,
            hostname=reg.router.hostname_for(app_name),
            src_dir=src_dir,
        )
        state.apps[app_name] = app
    old = app.replicas
    app.image, app.container_port, app.src_dir, app.replicas, app.hot_streak = (
        image,
        container_port,
        src_dir,
        [replica],
        0,
    )
    app.stateful = stateful
    app.events.append(
        {"ts": time.time(), "type": "deploy", "node": replica.node, "upstream": replica.upstream}
    )
    push_config(reg, state)
    save_state(state)
    for r in old:  # previous version's containers (other image) are no longer routed; free them
        if r.upstream != replica.upstream:
            try:
                nodepool.remove_container(reg.get(r.node), r.container)
            except KeyError:
                pass
    return f"http://{app.hostname}" + (f":{reg.router.public_port}" if reg.router.public_port != 80 else "")


def scale_out(app: App, reg: Registry, state: State, reason: str) -> Replica | None:
    used = {r.node for r in app.replicas}
    try:
        node, metrics = nodepool.select_node(reg, exclude=used)
    except RuntimeError as e:
        app.events.append({"ts": time.time(), "type": "scale_out_blocked", "reason": f"{reason}; {e}"})
        save_state(state)
        return None
    src = reg.get(app.replicas[0].node)
    secs = nodepool.relay_image(src, node, app.image)  # via this machine; nodes hold no SSH keys
    log.info("image %s available on %s (%.0fs)", app.image, node.name, secs)
    port = nodepool.free_port_on(node)
    cname = f"{app.name}-r{len(app.replicas) + 1}-{int(time.time())}"
    nodepool.run_container(node, app.image, cname, port, app.container_port)
    rep = Replica(node=node.name, container=cname, host=node.host, port=port)
    # only route to it once it answers
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"http://{rep.host}:{rep.port}/", timeout=3) as resp:
                if resp.status < 500:
                    break
        except Exception as e:  # noqa: BLE001 — not up yet
            log.debug("replica %s not ready: %s", rep.upstream, e)
        time.sleep(2)
    app.replicas.append(rep)
    app.hot_streak = 0
    app.events.append(
        {
            "ts": time.time(),
            "type": "scale_out",
            "node": node.name,
            "upstream": rep.upstream,
            "reason": reason,
            "candidates": nodepool.metrics_dict(metrics),
        }
    )
    push_config(reg, state)
    save_state(state)
    log.info("scale-out %s -> %s (%s)", app.name, node.name, reason)
    return rep


def scale_in(app: App, reg: Registry, state: State, reason: str) -> None:
    if len(app.replicas) <= 1:
        return
    rep = app.replicas.pop()  # newest first
    push_config(reg, state)  # stop routing before killing
    nodepool.remove_container(reg.get(rep.node), rep.container)
    app.events.append(
        {"ts": time.time(), "type": "scale_in", "node": rep.node, "upstream": rep.upstream, "reason": reason}
    )
    save_state(state)


def scale(app_name: str, n: int) -> App:
    reg, state = nodepool.load_registry(), load_state()
    app = state.apps[app_name]
    while len(app.replicas) < n:
        if scale_out(app, reg, state, "manual") is None:
            break
    while len(app.replicas) > n:
        scale_in(app, reg, state, "manual")
    return app


def remove(app_name: str) -> None:
    reg, state = nodepool.load_registry(), load_state()
    app = state.apps.pop(app_name, None)
    push_config(reg, state)
    save_state(state)
    for r in app.replicas if app else []:
        nodepool.remove_container(reg.get(r.node), r.container)


# --------------------------------------------------------------------------- #
# Watcher
# --------------------------------------------------------------------------- #
def sample(app: App, reg: Registry) -> list[tuple[Replica, float | None]]:
    """Average CPU% per replica since the previous sample (cgroup counters), falling back to docker stats."""
    out = []
    for r in app.replicas:
        node = reg.get(r.node)
        now = time.time()
        usage = nodepool.container_cpu_usage_usec(node, r.container)
        cpu: float | None
        if (
            usage is not None
            and r.last_usage_usec is not None
            and r.last_sample_ts
            and now > r.last_sample_ts
        ):
            cpu = (usage - r.last_usage_usec) / ((now - r.last_sample_ts) * 1e6) * 100
        elif usage is None:
            cpu = nodepool.container_cpu_percent(node, r.container)
        else:
            cpu = None  # first reading: no interval yet
        r.last_usage_usec, r.last_sample_ts, r.last_cpu = (
            usage,
            now,
            (round(cpu, 1) if cpu is not None else None),
        )
        out.append((r, cpu))
    return out


def tick(emit=None) -> list[dict]:
    """One monitoring pass. Returns a summary per app (also sent through `emit` for the dashboard)."""
    reg, state = nodepool.load_registry(), load_state()
    summary = []
    for app in list(state.apps.values()):
        samples = sample(app, reg)
        cpus = [c for _, c in samples if c is not None]
        hottest = max(cpus) if cpus else None
        # second signal: the node itself is saturated (1-min load per core), even if this container's share is modest
        node_hot = any(
            (m := nodepool.probe(reg.get(r.node))).ok and m.load1 / max(m.cpus, 1) >= NODE_LOAD_HOT
            for r, _ in samples
        )
        action = None
        if (hottest is not None and hottest >= CPU_HOT) or node_hot:
            app.hot_streak += 1
            if app.stateful:
                action = "hot but stateful (database on this node): not replicating"
            elif app.hot_streak >= HOT_STREAK and len(app.replicas) < MAX_REPLICAS:
                rep = scale_out(
                    app, reg, state, f"replica cpu {hottest:.0f}% >= {CPU_HOT:.0f}% x{HOT_STREAK}"
                )
                action = f"scale_out -> {rep.node}" if rep else "scale_out blocked"
        else:
            app.hot_streak = 0
            # scale in only when the newest replica has lived a while (avoid flapping)
            if (
                hottest is not None
                and hottest < CPU_COLD
                and len(app.replicas) > 1
                and time.time() - app.replicas[-1].created_at > 120
            ):
                scale_in(app, reg, state, f"all replicas cpu < {CPU_COLD:.0f}%")
                action = "scale_in"
        save_state(state)
        row = {
            "app": app.name,
            "hostname": app.hostname,
            "replicas": [{"node": r.node, "upstream": r.upstream, "cpu": r.last_cpu} for r in app.replicas],
            "hot_streak": app.hot_streak,
            "node_hot": node_hot,
            "action": action,
        }
        summary.append(row)
        if emit:
            emit(row)
    return summary


def watch(interval: float = 10.0, emit=None, stop: threading.Event | None = None) -> None:
    while not (stop and stop.is_set()):
        try:
            for row in tick(emit):
                reps = ", ".join(
                    f"{r['node']}:{r['cpu'] if r['cpu'] is not None else '?'}%" for r in row["replicas"]
                )
                print(
                    f"[{time.strftime('%H:%M:%S')}] {row['app']:<16} replicas={len(row['replicas'])} cpu=[{reps}] streak={row['hot_streak']} node_hot={row['node_hot']} {row['action'] or ''}",
                    flush=True,
                )
        except Exception as e:  # noqa: BLE001 — keep watching
            print(f"[watch] error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(interval)


def start_background_watcher(interval: float = 10.0, emit=None) -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=watch, args=(interval, emit, stop), daemon=True, name="fleet-watch").start()
    return stop


# --------------------------------------------------------------------------- #
# Demo load generator
# --------------------------------------------------------------------------- #
def stress(app_name: str, seconds: int = 60, concurrency: int = 32, path: str = "/") -> dict:
    """Demo load generator. One persistent keep-alive connection per thread (a new TCP connection per request
    exhausts the client's ephemeral ports within seconds and stalls — which looks exactly like a server outage)."""
    import http.client

    state = load_state()
    host = state.apps[app_name].hostname
    reg = nodepool.load_registry()
    port = reg.router.public_port if reg.router else 80
    counts: dict[str, int] = {}
    lock, deadline, total, errors, latencies = threading.Lock(), time.time() + seconds, [0], [0], []

    def worker():
        conn = None
        while time.time() < deadline:
            t0 = time.time()
            try:
                if conn is None:
                    conn = http.client.HTTPConnection(host, port, timeout=5)
                conn.request("GET", path, headers={"Connection": "keep-alive"})
                resp = conn.getresponse()
                resp.read()
                up = resp.getheader("X-Cloudmorph-Upstream", "?")
                with lock:
                    total[0] += 1
                    counts[up] = counts.get(up, 0) + 1
                    latencies.append(time.time() - t0)
            except Exception:  # noqa: BLE001 — count and reconnect
                with lock:
                    errors[0] += 1
                if conn is not None:
                    conn.close()
                conn = None
                time.sleep(0.05)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for t in threads:
        t.start()
    last = 0
    while time.time() < deadline:
        time.sleep(5)
        with lock:
            print(
                f"  {total[0]:>7} req  ({(total[0] - last) / 5:.0f} rps)  by upstream: {counts}  errors={errors[0]}",
                flush=True,
            )
            last = total[0]
    for t in threads:
        t.join(timeout=6)
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95)] * 1000 if latencies else None
    return {
        "requests": total[0],
        "errors": errors[0],
        "by_upstream": counts,
        "p95_ms": round(p95) if p95 else None,
    }


# --------------------------------------------------------------------------- #
def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m app.fleet")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-router")
    sub.add_parser("status")
    w = sub.add_parser("watch")
    w.add_argument("--interval", type=float, default=10)
    s = sub.add_parser("scale")
    s.add_argument("app")
    s.add_argument("n", type=int)
    rm = sub.add_parser("remove")
    rm.add_argument("app")
    st = sub.add_parser("stress")
    st.add_argument("app")
    st.add_argument("--seconds", type=int, default=60)
    st.add_argument("--concurrency", type=int, default=32)
    a = ap.parse_args(argv)
    reg = nodepool.load_registry()
    if a.cmd == "init-router":
        print("router:", init_router(reg))
    elif a.cmd == "status":
        state = load_state()
        for m in [nodepool.probe(n) for n in reg.nodes]:
            print(
                f"node {m.node:<12} ok={m.ok} load1={m.load1} containers={m.containers} score={m.score if m.ok else 'n/a'}"
            )
        for app in state.apps.values():
            print(f"app  {app.name:<12} http://{app.hostname}  replicas={[r.upstream for r in app.replicas]}")
            for e in app.events[-3:]:
                print(
                    f"     {time.strftime('%H:%M:%S', time.localtime(e['ts']))} {e['type']} {e.get('node', '')} {e.get('reason', '')}"
                )
    elif a.cmd == "watch":
        watch(a.interval)
    elif a.cmd == "scale":
        app = scale(a.app, a.n)
        print(app.name, "->", [r.upstream for r in app.replicas])
    elif a.cmd == "remove":
        remove(a.app)
        print("removed", a.app)
    elif a.cmd == "stress":
        print(json.dumps(stress(a.app, a.seconds, a.concurrency)))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
