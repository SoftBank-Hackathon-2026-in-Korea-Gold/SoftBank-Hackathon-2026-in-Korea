"""Bounded, private healing memory. Only verified Dockerfile edits are retained.

Entries contain hashes and an allowlisted edit, never stderr, source, credentials,
full Dockerfiles, or user/project names. Exact source and Dockerfile fingerprints
prevent applying a fix to a changed checkout. Memory failure never blocks healing.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)
MAX_PATCH_BYTES = 8192


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class MemoryScope:
    owner: str
    project: str
    source: str

    @property
    def fingerprint(self) -> str:
        return _hash(json.dumps([self.owner, self.project, self.source]))


def source_fingerprint(directory: str) -> str | None:
    """Hash the relevant checkout; oversized/unreadable trees disable reuse.

    Secret files are excluded and no file contents leave the hash function.
    Symlinks are not followed, including a symlinked root supplied by a caller.
    """
    from app.analyzer import SECRET_FILE

    # Analyzer exclusions are unsuitable here: build output, vendor packages and
    # hidden app configuration can all be copied into the deployed image. Ignore
    # only checkout metadata and the deployer's own changing runtime bookkeeping.
    skip_dirs = {".git", ".deployer"}

    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        return None
    digest = hashlib.sha256()
    count = size = 0

    def unreadable(error: OSError) -> None:
        raise error

    try:
        for parent, directories, files in os.walk(root, followlinks=False, onerror=unreadable):
            if any((Path(parent) / d).is_symlink() for d in directories if d not in skip_dirs):
                return None
            directories[:] = sorted(
                d for d in directories if d not in skip_dirs and not (Path(parent) / d).is_symlink()
            )
            for name in sorted(files):
                path = Path(parent) / name
                if SECRET_FILE.fullmatch(name) or path == root / "Dockerfile":
                    continue
                if path.is_symlink():
                    return None  # target content could change without this checkout changing
                count += 1
                size += path.stat().st_size
                if count > 512 or size > 16 * 1024 * 1024:
                    return None
                digest.update(path.relative_to(root).as_posix().encode() + b"\0")
                digest.update(path.read_bytes() + b"\0")
        return digest.hexdigest()
    except OSError:
        return None


def error_fingerprint(error_log: str) -> str:
    """Hash error evidence after normalising volatile timestamps and paths."""
    # Import locally to avoid an import cycle and keep classification authoritative.
    from app.healer import _section, classify_error

    runtime = _section(error_log, "runtime logs") or _section(error_log, "cloudrun revision logs")
    evidence = runtime or error_log
    lines = [line.strip() for line in evidence.splitlines() if line.strip()]
    causes = [line for line in lines if re.search(r"Error\b|Exception\b|Cannot find|failed to start", line)]
    evidence = "\n".join(causes[-3:] or lines[-3:])
    evidence = re.sub(r"\d{4}-\d\d-\d\dT[^\s]+\s*", "", evidence)
    evidence = re.sub(r"(?:/tmp/|/app/)[\w./-]+", "<path>", evidence)
    evidence = re.sub(r"\b[0-9a-f]{12,64}\b", "<id>", evidence)
    return _hash(classify_error(error_log).value + "|" + evidence)


# Only installation instructions with bare package names and fixed non-secret
# runtime settings are persisted. Arbitrary ENV/ARG, shell code, URLs, and CMDs
# are intentionally excluded. The healer still applies these fixes normally.
_PACKAGE = r"[A-Za-z][A-Za-z0-9_.-]{0,63}(?:==[0-9][A-Za-z0-9_.-]{0,31})?"
_SAFE_LINE = re.compile(
    rf"(?:RUN pip3? install (?:--no-cache-dir )?{_PACKAGE}(?: {_PACKAGE})*"
    rf"|RUN npm install (?:--save )?(?:@[a-z0-9_.-]+/)?[a-z0-9_.-]+"
    rf"|RUN apt-get update && apt-get install -y --no-install-recommends "
    rf"{_PACKAGE}(?: {_PACKAGE})* && rm -rf /var/lib/apt/lists/\*"
    r"|EXPOSE [0-9]{1,5}|ENV PORT=[0-9]{1,5}"
    r"|ENV PYTHONUNBUFFERED=1(?: PYTHONFAULTHANDLER=1)?)\n?"
)
_SECRET_TEXT = re.compile(
    r"secret|password|passwd|credential|api[_-]?key|access[_-]?token|private[_-]?key|"
    r"sk-[A-Za-z0-9]|gh[pousr]_|AKIA[A-Z0-9]|-----BEGIN",
    re.IGNORECASE,
)


def _safe_edits(before: str, after: str) -> list[list] | None:
    if max(len(before.encode()), len(after.encode())) > 64 * 1024:
        return None
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    edits = []
    for operation, start, end, next_start, next_end in difflib.SequenceMatcher(a=old, b=new).get_opcodes():
        if operation == "equal":
            continue
        replacement = new[next_start:next_end]
        # Deletions and arbitrary replacements can remove safety settings. Accept
        # only additions or replacements where every affected instruction is safe.
        if not replacement or any(
            not _SAFE_LINE.fullmatch(line) or _SECRET_TEXT.search(line)
            for line in [*old[start:end], *replacement]
        ):
            return None
        edits.append([start, end, replacement])
    if not edits or len(json.dumps(edits).encode()) > MAX_PATCH_BYTES:
        return None
    return edits


class MemoryStore:
    """SQLite transactions also protect writes from multiple worker processes."""

    def __init__(
        self, path: str | Path, *, ttl_days: float = 30, max_entries: int = 200, max_per_scope: int = 20
    ):
        self.path = Path(path)
        self.ttl = max(ttl_days, 0) * 86400
        self.max_entries = max(max_entries, 1)
        self.max_per_scope = max(max_per_scope, 1)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create privately before sqlite opens it. Do not truncate an existing DB.
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        conn = sqlite3.connect(self.path, timeout=2)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS fixes (key TEXT PRIMARY KEY, scope TEXT, after_hash TEXT, "
                "edits TEXT, used REAL)"
            )
            with conn:
                yield conn
        finally:
            conn.close()

    def _key(self, scope: MemoryScope, target: str, before: str, error: str, snapshot: str) -> str:
        return _hash(
            json.dumps([scope.fingerprint, target, _hash(before), error_fingerprint(error), snapshot])
        )

    def recall(self, scope: MemoryScope, target: str, before: str, error: str, snapshot: str) -> str | None:
        if target == "function":
            return None
        try:
            key = self._key(scope, target, before, error, snapshot)
            with self._connect() as conn:
                row = conn.execute("SELECT after_hash, edits, used FROM fixes WHERE key=?", (key,)).fetchone()
                if (
                    not row
                    or not isinstance(row[1], str)
                    or row[2] <= time.time() - self.ttl
                    or len(row[1].encode()) > MAX_PATCH_BYTES
                ):
                    return None
                lines = before.splitlines(keepends=True)
                edits = json.loads(row[1])
                for start, end, replacement in reversed(edits):
                    if not (
                        isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= len(lines)
                    ):
                        return None
                    lines[start:end] = replacement
                after = "".join(lines)
                if _hash(after) != row[0] or _safe_edits(before, after) != edits:
                    return None  # corrupt or tampered data cannot bypass the admission policy
                conn.execute("UPDATE fixes SET used=? WHERE key=?", (time.time(), key))
                return after
        except (OSError, sqlite3.Error, ValueError, TypeError, IndexError, KeyError, RecursionError):
            logger.warning("Healing memory unavailable; proceeding without reuse")
            return None

    def remember(
        self, scope: MemoryScope, target: str, before: str, after: str, error: str, snapshot: str
    ) -> bool:
        edits = _safe_edits(before, after)
        if edits is None or target == "function":
            return False
        try:
            with self._connect() as conn:
                now = time.time()
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM fixes WHERE used<=?", (now - self.ttl,))
                conn.execute(
                    "INSERT OR REPLACE INTO fixes VALUES (?, ?, ?, ?, ?)",
                    (
                        self._key(scope, target, before, error, snapshot),
                        scope.fingerprint,
                        _hash(after),
                        json.dumps(edits),
                        now,
                    ),
                )
                conn.execute(
                    "DELETE FROM fixes WHERE scope=? AND key NOT IN "
                    "(SELECT key FROM fixes WHERE scope=? ORDER BY used DESC LIMIT ?)",
                    (scope.fingerprint, scope.fingerprint, self.max_per_scope),
                )
                conn.execute(
                    "DELETE FROM fixes WHERE key NOT IN (SELECT key FROM fixes ORDER BY used DESC LIMIT ?)",
                    (self.max_entries,),
                )
            return True
        except (OSError, sqlite3.Error):
            logger.warning("Healing memory unavailable; fix was not retained")
            return False

    def forget(self, scope: MemoryScope, target: str, before: str, error: str, snapshot: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM fixes WHERE key=?", (self._key(scope, target, before, error, snapshot),)
                )
        except (OSError, sqlite3.Error):
            logger.warning("Healing memory unavailable; invalidation was deferred")


def configured_store() -> MemoryStore | None:
    if os.getenv("CLOUDMORPH_HEAL_MEMORY", "off").lower() not in {"1", "true", "on", "yes"}:
        return None
    path = os.getenv("CLOUDMORPH_HEAL_MEMORY_FILE") or (
        Path(__file__).resolve().parents[1] / ".cloudmorph" / "heal-memory.sqlite3"
    )
    try:
        days = float(os.getenv("CLOUDMORPH_HEAL_MEMORY_DAYS", "30"))
    except ValueError:
        days = 30
    return MemoryStore(path, ttl_days=days)
