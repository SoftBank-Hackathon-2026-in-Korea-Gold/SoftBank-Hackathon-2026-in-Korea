"""Offline regression tests for source isolation and API/SSE compatibility."""

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
def m(monkeypatch, tmp_path):
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
    # the project registry is persisted to disk; keep this private module's copy in tmp
    monkeypatch.setattr(module, "PROJECTS_FILE", tmp_path / "projects.json")
    module._projects.clear()

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
        assert prepared != str(source.resolve())
        assert (Path(prepared) / "app.py").read_text() == "x"
        assert not (Path(prepared) / "node_modules").exists()
        assert not (Path(prepared) / ".venv").exists()


@pytest.mark.parametrize("operation", ["prepare", "copy"])
def test_sensitive_files_are_excluded_from_build_source(m, tmp_path, operation):
    source = tmp_path / "source"
    (source / ".aws").mkdir(parents=True)
    (source / ".docker").mkdir()
    (source / ".terraform" / "providers").mkdir(parents=True)
    (source / ".config" / "gcloud").mkdir(parents=True)
    (source / ".config" / "gh").mkdir(parents=True)
    (source / ".ssh").mkdir()
    (source / ".env").write_text("dummy credential")
    (source / ".env.example").write_text("example only")
    (source / ".aws" / "credentials").write_text("dummy credential")
    (source / ".docker" / "config.json").write_text("dummy credential")
    (source / ".docker" / "Dockerfile").write_text("FROM scratch")
    (source / ".terraform" / "terraform.tfstate").write_text("dummy credential")
    (source / ".terraform" / "providers" / "README.md").write_text("project metadata")
    (source / ".config" / "gcloud" / "application_default_credentials.json").write_text("dummy credential")
    (source / ".config" / "gh" / "hosts.yml").write_text("dummy credential")
    (source / ".ssh" / "id_ed25519").write_text("dummy private key")
    (source / "service-account-demo.json").write_text("dummy credential")
    (source / "tls.pem").write_text("dummy private key")
    (source / "terraform.tfstate.backup").write_text("dummy credential")
    (source / ".env.production").write_text("dummy credential")
    (source / ".env.example").write_text("APP_ENV=development")
    (source / "app.py").write_text("print('safe')")

    if operation == "prepare":
        with m.prepare_source(str(source)) as prepared:
            sanitized = Path(prepared)
            assert (sanitized / "app.py").read_text() == "print('safe')"
            assert not (sanitized / ".env").exists()
            assert (sanitized / ".env.example").read_text() == "APP_ENV=development"
            assert not (sanitized / ".env.production").exists()
            assert not (sanitized / ".aws").exists()
            assert not (sanitized / ".docker" / "config.json").exists()
            assert (sanitized / ".docker" / "Dockerfile").read_text() == "FROM scratch"
            assert not (sanitized / ".terraform" / "terraform.tfstate").exists()
            assert (sanitized / ".terraform" / "providers" / "README.md").exists()
            assert not (sanitized / ".config" / "gcloud").exists()
            assert not (sanitized / ".config" / "gh").exists()
            assert not (sanitized / ".ssh").exists()
            assert not (sanitized / "service-account-demo.json").exists()
            assert not (sanitized / "tls.pem").exists()
            assert not (sanitized / "terraform.tfstate.backup").exists()
    else:
        sanitized = Path(m._copy_for_target(str(source), tmp_path / "build-context"))
        assert (sanitized / "app.py").read_text() == "print('safe')"
        assert (sanitized / ".env.example").read_text() == "APP_ENV=development"
        assert not any(
            (sanitized / relative).exists()
            for relative in (
                ".env.production",
                ".env",
                ".aws",
                ".docker/config.json",
                ".terraform/terraform.tfstate",
                ".config/gcloud",
                ".config/gh",
                ".ssh",
                "service-account-demo.json",
                "tls.pem",
                "terraform.tfstate.backup",
            )
        )
        assert (sanitized / ".docker" / "Dockerfile").read_text() == "FROM scratch"
        assert (sanitized / ".terraform" / "providers" / "README.md").exists()


def test_sensitive_files_are_not_extracted_from_zip(m, tmp_path):
    archive_path = tmp_path / "source.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("app.py", "safe")
        archive.writestr(".env", "dummy credential")
        archive.writestr(".env.production", "dummy credential")
        archive.writestr(".env.example", "APP_ENV=development")
        archive.writestr(".docker/config.json", "dummy credential")
        archive.writestr(".config/gh/hosts.yml", "dummy credential")
        archive.writestr(".terraform/terraform.tfstate", "dummy credential")
        archive.writestr(".terraform.lock.hcl", "provider lock")

    extracted = m._extract_zip_safe(archive_path, tmp_path / "extracted")

    assert (extracted / "app.py").read_text() == "safe"
    assert not (extracted / ".env").exists()
    assert not (extracted / ".env.production").exists()
    assert (extracted / ".env.example").read_text() == "APP_ENV=development"
    assert not (extracted / ".docker" / "config.json").exists()
    assert not (extracted / ".config" / "gh").exists()
    assert not (extracted / ".terraform" / "terraform.tfstate").exists()
    assert (extracted / ".terraform.lock.hcl").read_text() == "provider lock"


@pytest.mark.parametrize("member_name", ["../escape.txt", "/absolute.txt", "nested/../../escape.txt"])
def test_zip_path_traversal_and_absolute_paths_are_rejected(m, tmp_path, member_name):
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(member_name, "outside")
    destination = tmp_path / "extracted"
    destination.mkdir()

    with pytest.raises(ValueError, match="Unsafe ZIP entry"):
        m._extract_zip_safe(archive_path, destination)

    assert not (tmp_path / "escape.txt").exists()


def test_zip_symlink_entry_is_rejected(m, tmp_path):
    archive_path = tmp_path / "symlink.zip"
    symlink = zipfile.ZipInfo("link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(symlink, "../../outside")
    destination = tmp_path / "extracted"
    destination.mkdir()

    with pytest.raises(ValueError, match="Unsafe ZIP entry"):
        m._extract_zip_safe(archive_path, destination)

    assert not (tmp_path.parent / "outside").exists()


@pytest.mark.parametrize("limit", ["bytes", "entries"])
def test_zip_archive_limits_include_sensitive_entries(m, tmp_path, monkeypatch, limit):
    archive_path = tmp_path / "bounded.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(".env", "x" * 8)
        archive.writestr("app.py", "safe")
    setting = "MAX_ZIP_UNCOMPRESSED_BYTES" if limit == "bytes" else "MAX_ZIP_ENTRIES"
    monkeypatch.setattr(m, setting, 4 if limit == "bytes" else 1)

    with pytest.raises(ValueError, match="ZIP contents exceed"):
        m._extract_zip_safe(archive_path, tmp_path / "extracted")


@pytest.mark.parametrize("source_kind", ["local", "zip", "git"])
def test_filtered_source_limits_are_consistent(m, tmp_path, monkeypatch, source_kind):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".env").write_text("x" * 100)
    (source / "app.py").write_text("safe")
    if source_kind == "zip":
        archive_path = tmp_path / "source.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(".env", "x" * 100)
            archive.writestr("app.py", "safe")
        input_source = str(archive_path)
    elif source_kind == "git":

        def fake_clone(args, **kwargs):
            destination = Path(args[-1])
            destination.mkdir()
            (destination / ".env").write_text("x" * 100)
            (destination / "app.py").write_text("safe")
            return subprocess.CompletedProcess(args, 0)

        monkeypatch.setattr(m.subprocess, "run", fake_clone)
        input_source = "https://github.com/example/project"
    else:
        input_source = str(source)

    monkeypatch.setattr(m, "MAX_SOURCE_BYTES", 4)
    monkeypatch.setattr(m, "MAX_SOURCE_FILES", 1)
    with m.prepare_source(input_source) as prepared:
        prepared_path = Path(prepared)
        assert (prepared_path / "app.py").read_text() == "safe"
        assert not (prepared_path / ".env").exists()


def test_local_source_under_protected_root_is_rejected(m, tmp_path, monkeypatch):
    protected = tmp_path / "server"
    source = protected / "internal"
    source.mkdir(parents=True)
    (source / "app.py").write_text("not read")
    monkeypatch.setattr(m, "_LOCAL_SOURCE_DENY_ROOTS", (protected,))

    with pytest.raises(ValueError, match="protected system directory"), m.prepare_source(str(source)):
        pytest.fail("protected server path was accepted")


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

    def mkdtemp_in_test_dir(*args, **kwargs):
        if len(args) >= 3:
            args = (*args[:2], args[2] or tmp_path, *args[3:])
        else:
            kwargs["dir"] = kwargs.get("dir") or tmp_path
        return original_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(m.tempfile, "mkdtemp", mkdtemp_in_test_dir)
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
            assert status["status"] == "completed", status
            assert set(status["targets"]) == {"local", "cloudrun"}
            response = await routes["/deploy/{deployment_id}/events"](deployment_id)
            events = [event async for event in response.body_iterator]
            # analyzer summary/notes are streamed as "log" events; the stage/done backbone is unchanged
            assert [e["event"] for e in events if e["event"] != "log"] == [
                "stage",
                "stage",
                "done",
                "stage",
                "done",
            ]
            assert any(e["event"] == "log" and "analyzer:" in e["data"] for e in events)
            assert all(set(json.loads(e["data"])) == {"type", "stage", "payload", "ts"} for e in events)

    asyncio.run(scenario())
    assert len(paths) == len({p.name for p in paths}) == 4
    assert len({m.deployer.slug(p.name) for p in paths}) == 4
    for directory in paths:
        assert directory.name.startswith("cloudmorph-")
        if directory.name.endswith("-cloudrun"):
            assert not directory.exists()
        else:
            assert (directory / "app.py").read_text() == "dummy"


def test_cloudrun_workspace_is_removed_after_heal_finishes(m, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("dummy")
    initial_dockerfile = 'FROM python:3.11-slim\nCMD ["python", "app.py"]\n'
    monkeypatch.setattr(
        m.analyzer,
        "analyze",
        lambda _: AnalysisResult(target="cloudrun", language="python", dockerfile=initial_dockerfile),
    )
    deploy_attempts = []

    def fake_deploy(directory, dockerfile, target):
        assert Path(directory).exists()
        assert (Path(directory) / "app.py").read_text() == "dummy"
        deploy_attempts.append(dockerfile)
        if len(deploy_attempts) == 1:
            return DeployResult(
                success=False,
                target=target,
                exit_code=1,
                stderr="ModuleNotFoundError: No module named 'flask'",
            )
        return DeployResult(success=True, target=target, exit_code=0, url="https://example.invalid")

    monkeypatch.setattr(m.deployer, "deploy", fake_deploy)
    original_mkdtemp = m.tempfile.mkdtemp

    def mkdtemp_in_test_dir(*args, **kwargs):
        if len(args) >= 3:
            args = (*args[:2], args[2] or tmp_path, *args[3:])
        else:
            kwargs["dir"] = kwargs.get("dir") or tmp_path
        return original_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(m.tempfile, "mkdtemp", mkdtemp_in_test_dir)
    original_remove = m._remove_workspace
    removed_cloudrun_dirs = []

    def checked_remove(path):
        if path.name.endswith("-cloudrun"):
            assert len(deploy_attempts) == 2
            assert path.exists()
            removed_cloudrun_dirs.append(path)
        original_remove(path)

    monkeypatch.setattr(m, "_remove_workspace", checked_remove)
    routes = {route.path: route.endpoint for route in m.app.routes if hasattr(route, "endpoint")}

    async def scenario():
        response = await routes["/deploy"](DeployRequest(source=str(source), targets=["cloudrun"]))
        deployment_id = response["deployment_id"]

        async def wait_for_job():
            while not m._jobs[deployment_id].completed:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_for_job(), timeout=5)
        return await routes["/deploy/{deployment_id}"](deployment_id)

    status = asyncio.run(scenario())
    assert status["status"] == "completed"
    assert len(deploy_attempts) == 2
    assert "pip install --no-cache-dir flask" in deploy_attempts[1]
    assert len(removed_cloudrun_dirs) == 1
    assert not removed_cloudrun_dirs[0].exists()


def test_failed_deploy_heals_and_preserves_sse_contract(m, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("dummy")
    dockerfile = (
        "FROM python:3.11-slim\n"
        "WORKDIR /app\n"
        "COPY . .\n"
        "EXPOSE 5000\n"
        'CMD ["gunicorn", "-b", "127.0.0.1:5000", "app:app"]\n'
    )
    monkeypatch.setattr(
        m.analyzer,
        "analyze",
        lambda _: AnalysisResult(target="local", language="python", dockerfile=dockerfile),
    )
    deploy_attempts = []

    def fake_deploy(directory, attempted_dockerfile, target):
        deploy_attempts.append(attempted_dockerfile)
        if len(deploy_attempts) == 1:
            return DeployResult(
                success=False,
                target=target,
                exit_code=1,
                stderr="ModuleNotFoundError: No module named 'flask'",
            )
        return DeployResult(success=True, target=target, exit_code=0, url="http://127.0.0.1:8080")

    monkeypatch.setattr(m.deployer, "deploy", fake_deploy)
    original_mkdtemp = m.tempfile.mkdtemp

    def mkdtemp_in_test_dir(*args, **kwargs):
        if len(args) >= 3:
            args = (*args[:2], args[2] or tmp_path, *args[3:])
        else:
            kwargs["dir"] = kwargs.get("dir") or tmp_path
        return original_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(m.tempfile, "mkdtemp", mkdtemp_in_test_dir)
    routes = {route.path: route.endpoint for route in m.app.routes if hasattr(route, "endpoint")}

    async def scenario():
        response = await routes["/deploy"](DeployRequest(source=str(source), targets=["local"]))
        assert set(response) == {"deployment_id"}
        deployment_id = response["deployment_id"]

        async def wait_for_job():
            while not m._jobs[deployment_id].completed:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_for_job(), timeout=5)
        status = await routes["/deploy/{deployment_id}"](deployment_id)
        assert status["status"] == "completed", status
        events_response = await routes["/deploy/{deployment_id}/events"](deployment_id)
        events = [event async for event in events_response.body_iterator]
        return events

    all_events = asyncio.run(scenario())
    assert len(deploy_attempts) == 2
    assert "pip install --no-cache-dir flask" in deploy_attempts[1]
    # informational "log" lines (e.g. analyzer summary) may appear anywhere; the dashboard
    # renders them, so they are part of the contract but not of the stage ordering.
    assert all(set(json.loads(event["data"])) == {"type", "stage", "payload", "ts"} for event in all_events)
    events = [event for event in all_events if event["event"] != "log"]
    assert [event["event"] for event in events] == [
        "stage",
        "stage",
        "stage",
        "heal_diff",
        "stage",
        "done",
    ]
    decoded = [json.loads(event["data"]) for event in events]
    assert all(set(event) == {"type", "stage", "payload", "ts"} for event in decoded)
    assert decoded[2]["stage"] == "heal"
    assert decoded[3]["stage"] == "heal"
    assert decoded[4]["stage"] == "redeploy"
    assert decoded[5]["type"] == "done"
    assert decoded[5]["payload"]["success"] is True


def test_stop_takes_down_every_live_target_once(m, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("dummy")
    monkeypatch.setattr(
        m.analyzer,
        "analyze",
        lambda _: AnalysisResult(target="local", language="python", dockerfile="FROM scratch\n"),
    )
    monkeypatch.setattr(
        m.deployer,
        "deploy",
        lambda directory, dockerfile, target: DeployResult(
            success=True, target=target, exit_code=0, url=f"https://{target}.invalid"
        ),
    )
    monkeypatch.setattr(m.deployer, "last_handles", lambda d: {"dir": Path(d).name})
    torn = []
    monkeypatch.setattr(m.deployer, "teardown", lambda t, h: torn.append((t, h["dir"])) or [f"down {t}"])
    original_mkdtemp = m.tempfile.mkdtemp
    monkeypatch.setattr(m.tempfile, "mkdtemp", lambda *a, **kw: original_mkdtemp(dir=tmp_path, **kw))
    routes = {route.path: route.endpoint for route in m.app.routes if hasattr(route, "endpoint")}
    stop = routes["/projects/{name}/stop"]

    async def scenario():
        response = await routes["/deploy"](DeployRequest(source=str(source), name="demo"))
        deployment_id = response["deployment_id"]
        while not m._jobs[deployment_id].completed:
            await asyncio.sleep(0.001)
        assert set(m._projects["demo"]["live"]) == {"local", "cloudrun"}
        first = await stop("demo")
        second = await stop("demo")
        with pytest.raises(m.HTTPException) as unknown:
            await stop("nope")
        m._app_locks["demo"].acquire()
        with pytest.raises(m.HTTPException) as busy:
            await stop("demo")
        m._app_locks["demo"].release()
        return first, second, unknown.value.status_code, busy.value.status_code

    first, second, unknown, busy = asyncio.run(scenario())
    assert first == {
        "name": "demo",
        "stopped": {"local": ["down local"], "cloudrun": ["down cloudrun"]},
        "failed": {},
    }
    assert sorted(torn) == [("cloudrun", "demo-cloudrun"), ("local", "demo-local")]
    assert second["stopped"] == {}  # nothing left serving
    assert (unknown, busy) == (404, 409)
    project = m._projects["demo"]
    assert project["last_status"] == "stopped" and project["live"] == {} and project["urls"] == {}


def test_stop_keeps_targets_that_failed_to_come_down(m, monkeypatch):
    m._projects["demo"] = {
        "name": "demo",
        "last_status": "completed",
        "urls": {"local": "u1", "cloudrun": "u2"},
        "live": {"local": {}, "cloudrun": {"service": "demo-cloudrun"}},
    }

    def teardown(target, handles):
        if target == "cloudrun":
            raise RuntimeError("permission denied")
        return ["down"]

    monkeypatch.setattr(m.deployer, "teardown", teardown)
    routes = {route.path: route.endpoint for route in m.app.routes if hasattr(route, "endpoint")}
    result = asyncio.run(routes["/projects/{name}/stop"]("demo"))
    assert result["failed"] == {"cloudrun": "RuntimeError: permission denied"}
    project = m._projects["demo"]
    assert set(project["live"]) == {"cloudrun"} and project["urls"] == {"cloudrun": "u2"}
    assert project["last_status"] == "completed"


def _cancel_setup(m, tmp_path, monkeypatch, fake_deploy):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("dummy")
    monkeypatch.setattr(
        m.analyzer,
        "analyze",
        lambda _: AnalysisResult(
            target="local", language="python", dockerfile='FROM python:3.11-slim\nCMD ["python", "app.py"]\n'
        ),
    )
    monkeypatch.setattr(m.deployer, "deploy", fake_deploy)
    original_mkdtemp = m.tempfile.mkdtemp
    monkeypatch.setattr(m.tempfile, "mkdtemp", lambda *a, **kw: original_mkdtemp(dir=tmp_path, **kw))
    routes = {route.path: route.endpoint for route in m.app.routes if hasattr(route, "endpoint")}
    return source, routes


def _events(routes, deployment_id):
    async def collect():
        response = await routes["/deploy/{deployment_id}/events"](deployment_id)
        return [(e["event"], json.loads(e["data"])) async for e in response.body_iterator]

    return asyncio.run(collect())


def test_cancel_kills_the_build_and_skips_the_remaining_targets(m, tmp_path, monkeypatch):
    building, released = threading.Event(), threading.Event()
    calls, killed = [], []

    def fake_deploy(directory, dockerfile, target):
        calls.append(target)
        building.set()
        assert released.wait(5)
        return DeployResult(success=False, target=target, exit_code=1, stderr="docker build killed")

    def fake_pkill(cmd, **kwargs):
        killed.append(cmd)
        released.set()
        return subprocess.CompletedProcess(cmd, 0)

    source, routes = _cancel_setup(m, tmp_path, monkeypatch, fake_deploy)
    monkeypatch.setattr(m.subprocess, "run", fake_pkill)

    async def scenario():
        deployment_id = (await routes["/deploy"](DeployRequest(source=str(source), name="demo")))[
            "deployment_id"
        ]
        while not building.is_set():
            await asyncio.sleep(0.001)
        assert await routes["/deploy/{deployment_id}/cancel"](deployment_id) == {
            "deployment_id": deployment_id,
            "cancelled": True,
        }
        while not m._jobs[deployment_id].completed:
            await asyncio.sleep(0.001)
        with pytest.raises(m.HTTPException) as late:
            await routes["/deploy/{deployment_id}/cancel"](deployment_id)
        return deployment_id, await routes["/deploy/{deployment_id}"](deployment_id), late.value.status_code

    deployment_id, status, late = asyncio.run(scenario())
    assert calls == ["local"]  # cloudrun never started
    assert killed[0][:2] == ["pkill", "-f"] and "cloudmorph-deploy-" in killed[0][2]
    assert status["status"] == "cancelled" and late == 409
    assert m._projects["demo"]["last_status"] == "cancelled"
    events = _events(routes, deployment_id)
    assert events[-1] == (
        "error",
        {**events[-1][1], "payload": {"message": "cancelled by user", "cancelled": True}},
    )
    assert not any(e == "stage" and d["stage"] == "heal" for e, d in events)  # healer never ran


def test_cancel_stops_the_healer_before_its_next_redeploy(m, tmp_path, monkeypatch):
    calls = []

    def fake_deploy(directory, dockerfile, target):
        calls.append(target)
        if len(calls) == 2:  # healer's first redeploy is in flight when the user cancels
            next(iter(m._jobs.values())).cancel.set()
            return DeployResult(
                success=False,
                target=target,
                exit_code=1,
                stderr="ModuleNotFoundError: No module named 'redis'",
            )
        return DeployResult(
            success=False, target=target, exit_code=1, stderr="ModuleNotFoundError: No module named 'flask'"
        )

    source, routes = _cancel_setup(m, tmp_path, monkeypatch, fake_deploy)

    async def scenario():
        deployment_id = (
            await routes["/deploy"](DeployRequest(source=str(source), targets=["local"], name="demo"))
        )["deployment_id"]
        while not m._jobs[deployment_id].completed:
            await asyncio.sleep(0.001)
        return deployment_id, await routes["/deploy/{deployment_id}"](deployment_id)

    deployment_id, status = asyncio.run(scenario())
    assert len(calls) == 2
    assert status["status"] == "cancelled"
    assert _events(routes, deployment_id)[-1][1]["payload"]["cancelled"] is True
