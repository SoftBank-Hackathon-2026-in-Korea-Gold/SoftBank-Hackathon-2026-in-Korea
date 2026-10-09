"""Offline regression tests for source isolation and API/SSE compatibility.

The 21 parametrized cases never load .env or invoke real Git, deployers or LLMs.
"""

import asyncio
import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.schemas import AnalysisResult, DeployRequest, DeployResult


@pytest.fixture
def m(monkeypatch):
    """Load an isolated orchestrator without .env access or real deployments."""
    monkeypatch.setattr("dotenv.load_dotenv", lambda: False)
    # Git preparation copies os.environ, so expose only synthetic settings.
    # Replace the mapping without inspecting any existing credential values.
    monkeypatch.setattr(
        os,
        "environ",
        {
            "CLOUDMORPH_GIT_HOSTS": "github.com",
            "CLOUDMORPH_CORS_ORIGINS": "http://localhost:5173",
        },
    )

    # A private module keeps app.main and its job registry untouched.
    module_name = "_cloudmorph_main_security"
    source_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)

    def forbidden(*args, **kwargs):
        raise AssertionError("Real commands, deployments and LLM calls are forbidden")

    monkeypatch.setattr(module.subprocess, "run", forbidden)
    monkeypatch.setattr(module.subprocess, "Popen", forbidden)
    monkeypatch.setattr(module.deployer, "deploy", forbidden)
    monkeypatch.setattr(module.healer, "default_llm_patch", forbidden)
    return module


@pytest.mark.parametrize("kind", ["file", "directory", "dangling"])
@pytest.mark.parametrize("operation", ["prepare", "copy"])
def test_links_rejected(m, tmp_path, kind, operation):
    source = tmp_path / "source"
    source.mkdir()
    external = tmp_path / "external"
    if kind == "file":
        external.write_text("external sentinel")
    elif kind == "directory":
        external.mkdir()
        (external / "secret").write_text("external sentinel")
    (source / "link").symlink_to(external, target_is_directory=kind == "directory")
    destination = tmp_path / "output"
    with pytest.raises(ValueError, match="symlink"):
        if operation == "prepare":
            with m.prepare_source(str(source)):
                pytest.fail("unsafe source yielded")
        else:
            m._copy_for_target(str(source), destination)
    assert not (destination / "link").exists()
    if kind == "file":
        assert external.read_text() == "external sentinel"


def test_symlink_root_rejected(m, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError), m.prepare_source(str(alias)):
        pytest.fail("symlink root accepted")


def test_link_swap_after_stat_cannot_read_external_file(m, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    victim = source / "app.py"
    victim.write_text("safe")
    external = tmp_path / "external"
    external.write_text("external sentinel")
    original_open = os.open

    def swap_then_open(path, flags, *args, **kwargs):
        if path == "app.py" and kwargs.get("dir_fd") is not None:
            victim.unlink()
            victim.symlink_to(external)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(m.os, "open", swap_then_open)
    destination = tmp_path / "output"
    with pytest.raises(OSError):
        m._copy_for_target(str(source), destination)
    assert not (destination / "app.py").exists()


def test_directory_link_swap_cannot_traverse_external(m, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    victim = source / "nested"
    victim.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret").write_text("external sentinel")
    original_open = os.open

    def swap_then_open(path, flags, *args, **kwargs):
        if path == "nested" and kwargs.get("dir_fd") is not None:
            victim.rmdir()
            victim.symlink_to(external, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(m.os, "open", swap_then_open)
    destination = tmp_path / "output"
    with pytest.raises(OSError):
        m._copy_for_target(str(source), destination)
    assert not (destination / "nested" / "secret").exists()


def test_fifo_rejected(m, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    os.mkfifo(source / "pipe")
    with pytest.raises(ValueError, match="non-regular"):
        m._copy_for_target(str(source), tmp_path / "output")


@pytest.mark.parametrize("limit", ["bytes", "count"])
@pytest.mark.parametrize("operation", ["prepare", "copy"])
def test_local_limits(m, tmp_path, monkeypatch, limit, operation):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"1234")
    (source / "b").write_bytes(b"5678")
    monkeypatch.setattr(
        m, "MAX_SOURCE_BYTES" if limit == "bytes" else "MAX_SOURCE_FILES", 7 if limit == "bytes" else 1
    )
    with pytest.raises(ValueError, match="limit"):
        if operation == "prepare":
            with m.prepare_source(str(source)):
                pytest.fail("oversized source accepted")
        else:
            m._copy_for_target(str(source), tmp_path / "output")


def test_prepare_skips_ignored_directories(m, tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "node_modules" / "pkg").mkdir(parents=True)
    (source / "node_modules" / "pkg" / "a").write_bytes(b"12345")
    (source / "node_modules" / "pkg" / "b").touch()
    (source / ".venv" / "bin").mkdir(parents=True)
    (source / ".venv" / "bin" / "python").symlink_to(tmp_path / "outside")
    (source / "app.py").write_text("x")
    monkeypatch.setattr(m, "MAX_SOURCE_FILES", 1)
    monkeypatch.setattr(m, "MAX_SOURCE_BYTES", 4)
    with m.prepare_source(str(source)) as prepared:
        assert prepared == str(source.resolve())


@pytest.mark.parametrize("kind", ["bytes", "count", "symlink"])
def test_mock_git_rejected_before_yield(m, tmp_path, monkeypatch, kind):
    clone_paths = []

    def fake_clone(args, **kwargs):
        destination = Path(args[-1])
        clone_paths.append(destination)
        destination.mkdir()
        if kind == "bytes":
            (destination / "a").write_bytes(b"12345")
            monkeypatch.setattr(m, "MAX_SOURCE_BYTES", 4)
        elif kind == "count":
            (destination / "a").touch()
            (destination / "b").touch()
            monkeypatch.setattr(m, "MAX_SOURCE_FILES", 1)
        else:
            (destination / "link").symlink_to(tmp_path / "outside")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(m.subprocess, "run", fake_clone)
    with pytest.raises(ValueError), m.prepare_source("https://github.com/example/project"):
        pytest.fail("unsafe clone yielded")
    assert clone_paths and not clone_paths[0].exists()


def test_git_clone_concurrency_is_bounded(m, monkeypatch):
    assert m.MAX_CONCURRENT_GIT_CLONES == 2
    monkeypatch.setattr(m, "_git_clone_semaphore", threading.BoundedSemaphore(2))
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_clone(args, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                started.set()
        try:
            if not release.wait(timeout=5):
                raise TimeoutError("test clone was not released")
            Path(args[-1]).mkdir()
            return subprocess.CompletedProcess(args, 0)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(m.subprocess, "run", fake_clone)

    def clone():
        with m.prepare_source("https://github.com/example/project"):
            pass

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(clone) for _ in range(3)]
        try:
            assert started.wait(timeout=3)
        finally:
            release.set()
        for future in futures:
            future.result(timeout=5)

    assert max_active == 2


def test_git_clone_concurrency_limit_is_environment_configurable(m, monkeypatch):
    monkeypatch.setitem(os.environ, "CLOUDMORPH_MAX_CONCURRENT_GIT_CLONES", "3")
    module_name = "_cloudmorph_main_security_config"
    source_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)

    assert module.MAX_CONCURRENT_GIT_CLONES == 3
    acquired = [module._git_clone_semaphore.acquire(blocking=False) for _ in range(4)]
    try:
        assert acquired == [True, True, True, False]
    finally:
        for acquired_slot in acquired:
            if acquired_slot:
                module._git_clone_semaphore.release()


def test_git_clone_capacity_returns_clear_error_when_saturated(m, monkeypatch):
    monkeypatch.setattr(m, "_git_clone_semaphore", threading.BoundedSemaphore(1))
    monkeypatch.setattr(m, "GIT_CLONE_WAIT_SECONDS", 0.01)
    started = threading.Event()
    release = threading.Event()

    def fake_clone(args, **kwargs):
        started.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test clone was not released")
        Path(args[-1]).mkdir()
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(m.subprocess, "run", fake_clone)

    def first_clone():
        with m.prepare_source("https://github.com/example/project"):
            pass

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(first_clone)
        try:
            assert started.wait(timeout=3)
            with (
                pytest.raises(ValueError, match="Git clone capacity reached"),
                m.prepare_source("https://github.com/example/project"),
            ):
                pytest.fail("saturated clone unexpectedly started")
        finally:
            release.set()
        future.result(timeout=5)


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        (subprocess.CalledProcessError(1, ["git", "clone"]), ValueError),
        (subprocess.TimeoutExpired(["git", "clone"], 120), ValueError),
        (RuntimeError("unexpected clone error"), RuntimeError),
    ],
    ids=["git-failure", "timeout", "unexpected-exception"],
)
def test_git_clone_semaphore_released_after_failure(m, monkeypatch, failure, expected_error):
    monkeypatch.setattr(m, "_git_clone_semaphore", threading.BoundedSemaphore(1))
    calls = 0

    def fake_clone(args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure
        Path(args[-1]).mkdir()
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(m.subprocess, "run", fake_clone)

    with pytest.raises(expected_error), m.prepare_source("https://github.com/example/project"):
        pytest.fail("failed clone unexpectedly yielded")
    with m.prepare_source("https://github.com/example/project"):
        pass

    assert calls == 2


@pytest.mark.parametrize("limit", ["bytes", "count"])
def test_zip_limits(m, tmp_path, monkeypatch, limit):
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("a", "1234")
        z.writestr("b", "5678")
    monkeypatch.setattr(
        m, "MAX_SOURCE_BYTES" if limit == "bytes" else "MAX_SOURCE_FILES", 7 if limit == "bytes" else 1
    )
    with pytest.raises(ValueError, match="limit"), m.prepare_source(str(archive)):
        pytest.fail("oversized zip accepted")


def test_copy_preserves_content_mode_and_cache_exclusion(m, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "nested").mkdir()
    script = source / "nested" / "start.sh"
    script.write_bytes(b"#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("dummy")
    output = tmp_path / "output"
    m._copy_for_target(str(source), output)
    assert (output / "nested" / "start.sh").read_bytes() == script.read_bytes()
    assert stat.S_IMODE((output / "nested" / "start.sh").stat().st_mode) == 0o755
    assert not (output / ".git").exists()


def test_two_jobs_names_api_status_and_sse(m, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("dummy")
    monkeypatch.setattr(
        m.analyzer,
        "analyze",
        lambda _: AnalysisResult(target="local", language="python", dockerfile="FROM scratch\n"),
    )
    paths = []

    def fake_deploy(directory, dockerfile, target):
        paths.append(Path(directory))
        return DeployResult(success=True, target=target, exit_code=0, url="https://example.invalid")

    monkeypatch.setattr(m.deployer, "deploy", fake_deploy)
    original_mkdtemp = m.tempfile.mkdtemp
    monkeypatch.setattr(m.tempfile, "mkdtemp", lambda **kwargs: original_mkdtemp(dir=tmp_path, **kwargs))
    routes = {route.path: route.endpoint for route in m.app.routes if hasattr(route, "endpoint")}

    async def scenario():
        assert await routes["/health"]() == {"status": "ok"}
        ids = []
        for _ in range(2):
            response = await routes["/deploy"](DeployRequest(source=str(source)))
            assert set(response) == {"deployment_id"}
            ids.append(response["deployment_id"])

        async def wait_for_jobs():
            while not all(m._jobs[i].completed for i in ids):
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_for_jobs(), timeout=5)
        for deployment_id in ids:
            status = await routes["/deploy/{deployment_id}"](deployment_id)
            assert status["status"] == "completed"
            assert set(status["targets"]) == {"local", "cloudrun"}
            response = await routes["/deploy/{deployment_id}/events"](deployment_id)
            events = [event async for event in response.body_iterator]
            assert [e["event"] for e in events] == ["stage", "stage", "done", "stage", "done"]
            assert all(set(json.loads(e["data"])) == {"type", "stage", "payload", "ts"} for e in events)

    asyncio.run(scenario())
    assert len(paths) == len({p.name for p in paths}) == 4
    assert len({m.deployer.slug(p.name) for p in paths}) == 4
    for directory in paths:
        assert directory.name.startswith("cloudmorph-")
        assert (directory / "app.py").read_text() == "dummy"
