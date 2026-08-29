"""Internal lock, source-fingerprint, worktree, and patch-checkpoint mechanics."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LockUnavailable(RuntimeError):
    pass


class ConcurrentSourceChange(RuntimeError):
    pass


class PatchPolicyViolation(RuntimeError):
    pass


class ProjectLock:
    """One atomic directory lock with ownership evidence and stale recovery."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.path = self.root / ".autodev" / "locks" / "project.lock"
        self.owner_id: str | None = None

    def acquire(self, *, recover_stale: bool = False) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        owner = {
            "owner_id": uuid.uuid4().hex,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "created_at": _now(),
            "heartbeat_at": _now(),
        }
        try:
            self.path.mkdir()
        except FileExistsError:
            existing = self._owner()
            same_host_dead = (
                existing.get("hostname") == socket.gethostname()
                and isinstance(existing.get("pid"), int)
                and not _pid_alive(existing["pid"])
            )
            if not recover_stale or not same_host_dead:
                raise LockUnavailable(f"project lock is live or not safely recoverable: {existing}")
            stale = (
                self.root / ".autodev" / "runs"
                / f"lock-recovery-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex}"
            )
            stale.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(self.path, stale)
                self.path.mkdir()
            except OSError as error:
                raise LockUnavailable(f"stale lock recovery lost the race: {error}") from error
            owner["recovered_from"] = stale.name
        _write_json_atomic(self.path / "owner.json", owner)
        self.owner_id = owner["owner_id"]
        return owner

    def _owner(self) -> dict[str, Any]:
        try:
            value = json.loads((self.path / "owner.json").read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"invalid_owner": value}
        except (OSError, json.JSONDecodeError) as error:
            return {"unreadable_owner": str(error)}

    def heartbeat(self) -> None:
        if self.owner_id is None:
            raise LockUnavailable("lock is not owned by this instance")
        owner = self._owner()
        if owner.get("owner_id") != self.owner_id:
            raise LockUnavailable("lock ownership changed")
        owner["heartbeat_at"] = _now()
        _write_json_atomic(self.path / "owner.json", owner)

    def release(self) -> None:
        if self.owner_id is None:
            return
        owner = self._owner()
        if owner.get("owner_id") != self.owner_id:
            raise LockUnavailable("refusing to release a lock owned by another process")
        tombstone = self.path.parent / f"released-{self.owner_id}"
        os.replace(self.path, tombstone)
        shutil.rmtree(tombstone)
        self.owner_id = None

    def __enter__(self) -> ProjectLock:
        self.acquire()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.release()


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    digest: str
    head: str
    files: int

    def to_dict(self) -> dict[str, Any]:
        return {"algorithm": "sha256", "digest": self.digest, "head": self.head, "files": self.files}


def _git(root: Path, *arguments: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments], cwd=root, input=input_bytes, check=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def source_fingerprint(root: Path) -> SourceFingerprint:
    """Hash HEAD and source bytes while excluding AutoDev's own runtime tree."""

    root = root.resolve()
    head_result = _git(root, "rev-parse", "HEAD")
    if head_result.returncode != 0:
        raise RuntimeError(head_result.stderr.decode(errors="replace").strip() or "not a Git repository")
    head = head_result.stdout.decode().strip()
    digest = hashlib.sha256()
    digest.update(head.encode())
    count = 0
    listed = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed.returncode != 0:
        raise RuntimeError(listed.stderr.decode(errors="replace"))
    relative_paths = sorted(
        Path(item.decode("utf-8", errors="surrogateescape"))
        for item in listed.stdout.split(b"\0") if item
    )
    for relative in relative_paths:
        if relative.parts[0] in {".git", ".autodev"} or relative.parts[0].startswith(".agent.v1-frozen-"):
            continue
        path = root / relative
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            digest.update(relative.as_posix().encode("utf-8", errors="surrogateescape"))
            digest.update(b"<missing>")
            continue
        digest.update(relative.as_posix().encode("utf-8", errors="surrogateescape"))
        digest.update(str(stat.S_IMODE(metadata.st_mode)).encode())
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif stat.S_ISREG(metadata.st_mode):
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        count += 1
    return SourceFingerprint(digest.hexdigest(), head, count)


def _changed_paths(worktree: Path) -> list[str]:
    result = _git(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace"))
    paths: list[str] = []
    entries = result.stdout.split(b"\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        text = entry.decode("utf-8", errors="surrogateescape")
        status_code, path = text[:2], text[3:]
        if status_code[0] in {"R", "C"}:
            if index < len(entries) and entries[index]:
                path = entries[index].decode("utf-8", errors="surrogateescape")
                index += 1
        paths.append(path)
    return sorted(set(paths))


def git_baseline_status(root: Path) -> dict[str, Any]:
    """Report whether a project has a commit and no non-AutoDev source changes."""

    root = root.resolve()
    try:
        head_result = _git(root, "rev-parse", "--verify", "HEAD")
    except OSError as error:
        return {
            "ready": False, "has_head": False, "clean": False,
            "head": None, "dirty_paths": [], "error": str(error),
        }
    if head_result.returncode != 0:
        detail = head_result.stderr.decode("utf-8", errors="replace").strip()
        return {
            "ready": False, "has_head": False, "clean": False,
            "head": None, "dirty_paths": [],
            "error": f"Git HEAD is missing: {detail}" if detail else "Git HEAD is missing",
        }
    try:
        dirty_paths = [
            path for path in _changed_paths(root)
            if Path(path).parts and Path(path).parts[0] != ".autodev"
        ]
    except (OSError, RuntimeError) as error:
        return {
            "ready": False, "has_head": True, "clean": False,
            "head": head_result.stdout.decode().strip(), "dirty_paths": [], "error": str(error),
        }
    return {
        "ready": not dirty_paths,
        "has_head": True,
        "clean": not dirty_paths,
        "head": head_result.stdout.decode().strip(),
        "dirty_paths": dirty_paths,
        "error": None,
    }


def validate_changed_paths(
    paths: Iterable[str], allowed: Iterable[str], protected: Iterable[str]
) -> None:
    allowed_patterns = tuple(allowed)
    protected_patterns = tuple(protected)
    violations: list[str] = []
    for path in paths:
        if any(fnmatch.fnmatchcase(path, pattern) or path == pattern.rstrip("/**") for pattern in protected_patterns):
            violations.append(f"protected:{path}")
        elif not any(fnmatch.fnmatchcase(path, pattern) or path == pattern.rstrip("/**") for pattern in allowed_patterns):
            violations.append(f"outside-allowed:{path}")
    if violations:
        raise PatchPolicyViolation(", ".join(violations))


@dataclass(slots=True)
class GitWorkspace:
    root: Path
    run_id: str
    path: Path | None = None
    baseline: SourceFingerprint | None = None

    def create(self) -> Path:
        self.root = self.root.resolve()
        self.baseline = source_fingerprint(self.root)
        self.path = self.root / ".autodev" / "workspaces" / self.run_id
        if self.path.exists():
            raise RuntimeError(f"workspace already exists: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        result = _git(self.root, "worktree", "add", "--detach", str(self.path), self.baseline.head)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="replace").strip())
        return self.path

    def collect_patch(
        self,
        *,
        allowed_paths: Iterable[str],
        protected_paths: Iterable[str] = (".autodev/**", ".git/**", ".codex/config.toml"),
    ) -> tuple[bytes, list[str]]:
        if self.path is None:
            raise RuntimeError("workspace has not been created")
        paths = _changed_paths(self.path)
        validate_changed_paths(paths, allowed_paths, protected_paths)
        add = _git(self.path, "add", "-N", "--", ".")
        if add.returncode != 0:
            raise RuntimeError(add.stderr.decode(errors="replace"))
        diff = _git(self.path, "diff", "--binary", "--full-index", "HEAD", "--")
        if diff.returncode != 0:
            raise RuntimeError(diff.stderr.decode(errors="replace"))
        return diff.stdout, paths

    def checkpoint(self, patch: bytes, paths: Iterable[str]) -> Path:
        if self.baseline is None:
            raise RuntimeError("workspace has no baseline")
        run_dir = self.root / ".autodev" / "runs" / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        patch_path = run_dir / "checkpoint.patch"
        patch_path.write_bytes(patch)
        metadata = {
            "run_id": self.run_id,
            "created_at": _now(),
            "patch_sha256": hashlib.sha256(patch).hexdigest(),
            "changed_paths": sorted(paths),
            "source_fingerprint": self.baseline.to_dict(),
        }
        _write_json_atomic(run_dir / "checkpoint.json", metadata)
        return patch_path

    def apply(self, patch: bytes) -> None:
        if self.baseline is None:
            raise RuntimeError("workspace has no baseline")
        current = source_fingerprint(self.root)
        if current.digest != self.baseline.digest:
            raise ConcurrentSourceChange(
                f"source changed concurrently: expected {self.baseline.digest}, found {current.digest}"
            )
        if not patch:
            return
        check = subprocess.run(
            ["git", "apply", "--check", "--binary", "-"], cwd=self.root, input=patch,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if check.returncode != 0:
            raise ConcurrentSourceChange(check.stderr.decode(errors="replace").strip())
        applied = subprocess.run(
            ["git", "apply", "--binary", "-"], cwd=self.root, input=patch,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if applied.returncode != 0:
            raise RuntimeError(applied.stderr.decode(errors="replace").strip())

    def cleanup(self) -> None:
        if self.path is None or not self.path.exists():
            return
        result = _git(self.root, "worktree", "remove", "--force", str(self.path))
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="replace").strip())
        _git(self.root, "worktree", "prune")
        self.path = None


def recover_stale_workspaces(root: Path) -> list[dict[str, Any]]:
    """Preserve a patch from every abandoned AutoDev worktree, then unregister it."""

    root = root.resolve()
    parent = root / ".autodev" / "workspaces"
    recovered: list[dict[str, Any]] = []
    if not parent.is_dir():
        return recovered
    for path in sorted(parent.iterdir()):
        if not path.is_dir():
            continue
        run_id = path.name
        add = _git(path, "add", "-N", "--", ".")
        diff = _git(path, "diff", "--binary", "--full-index", "HEAD", "--") if add.returncode == 0 else add
        run_dir = root / ".autodev" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        patch = diff.stdout if diff.returncode == 0 else b""
        (run_dir / "recovered.patch").write_bytes(patch)
        removal = _git(root, "worktree", "remove", "--force", str(path))
        item = {
            "run_id": run_id, "path": str(path), "patch_sha256": hashlib.sha256(patch).hexdigest(),
            "removed": removal.returncode == 0, "recovered_at": _now(),
        }
        _write_json_atomic(run_dir / "recovery.json", item)
        recovered.append(item)
    _git(root, "worktree", "prune")
    return recovered
