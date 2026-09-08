"""Self-update logic for `atc upgrade`.

Deliberately repo-agnostic: everything is derived from the local git clone's
own remote/branch config, never a hardcoded URL. This means a fork just
works — `atc upgrade` in a fork pulls from *that fork's* origin, not
damyanmp/atc, with zero configuration required. An explicit --remote-url
override exists for the rarer case of pulling from a remote that isn't
currently configured (e.g. syncing a fork against upstream).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class NotAGitRepoError(RuntimeError):
    """Raised when the atc install directory isn't a git checkout."""


class DirtyWorkingTreeError(RuntimeError):
    """Raised when local changes would be clobbered by a pull."""


@dataclass
class UpgradeStatus:
    repo_root: Path
    remote_name: str
    remote_url: str
    branch: str
    current_commit: str
    latest_commit: str
    commits_behind: int
    up_to_date: bool
    dirty: bool
    dirty_files: list[str] = field(default_factory=list)


def repo_root(start: Path | None = None) -> Path:
    """Return the atc install directory (repo root), given this file's location.

    src/lib/selfupdate.py -> parent.parent.parent = repo root (same convention
    as config.py's default config.yaml lookup).
    """
    if start is not None:
        return start
    return Path(__file__).resolve().parent.parent.parent


def _run(args: list[str], cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
    )


def is_git_repo(root: Path) -> bool:
    return (root / ".git").exists()


def current_branch(root: Path) -> str:
    r = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
    return r.stdout.strip() if r.returncode == 0 else ""


def remote_name_for_branch(root: Path, branch: str) -> str:
    """Return the upstream remote name for `branch` (usually 'origin')."""
    r = _run(["git", "config", f"branch.{branch}.remote"], root)
    name = r.stdout.strip() if r.returncode == 0 else ""
    return name or "origin"


def remote_url(root: Path, remote: str) -> str:
    r = _run(["git", "remote", "get-url", remote], root)
    return r.stdout.strip() if r.returncode == 0 else ""


def dirty_files(root: Path) -> list[str]:
    r = _run(["git", "status", "--porcelain"], root)
    if r.returncode != 0:
        return []
    return [line[3:] for line in r.stdout.splitlines() if line.strip()]


def commit_hash(root: Path, ref: str = "HEAD") -> str:
    r = _run(["git", "rev-parse", "--short", ref], root)
    return r.stdout.strip() if r.returncode == 0 else ""


def fetch(root: Path, remote: str, timeout: int = 30) -> None:
    r = _run(["git", "fetch", remote], root, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"git fetch {remote} failed: {r.stderr.strip()}")


def commits_behind(root: Path, branch: str, remote: str) -> int:
    r = _run(["git", "rev-list", "--count", f"HEAD..{remote}/{branch}"], root)
    if r.returncode != 0:
        return 0
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


def check_for_updates(root: Path | None = None, remote_url_override: str | None = None) -> UpgradeStatus:
    """Fetch from the remote and report how far behind the local checkout is.

    Never modifies the working tree. Raises NotAGitRepoError if `root` isn't
    a git checkout.
    """
    root = repo_root(root)
    if not is_git_repo(root):
        raise NotAGitRepoError(
            f"{root} is not a git checkout — can't self-update. "
            f"Re-clone from your git remote instead."
        )

    branch = current_branch(root)
    remote = remote_name_for_branch(root, branch)
    url = remote_url_override or remote_url(root, remote)
    dirty = dirty_files(root)

    fetch(root, remote)

    current = commit_hash(root)
    latest = commit_hash(root, f"{remote}/{branch}")
    behind = commits_behind(root, branch, remote)

    return UpgradeStatus(
        repo_root=root,
        remote_name=remote,
        remote_url=url,
        branch=branch,
        current_commit=current,
        latest_commit=latest,
        commits_behind=behind,
        up_to_date=(behind == 0),
        dirty=bool(dirty),
        dirty_files=dirty,
    )


def changelog_since(root: Path, from_ref: str, to_ref: str, limit: int = 20) -> list[str]:
    """One-line commit summaries between two refs, newest first."""
    r = _run(["git", "log", "--oneline", f"{from_ref}..{to_ref}", f"-{limit}"], root)
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines() if line.strip()]


def pull(root: Path, remote: str, branch: str, timeout: int = 60) -> None:
    """Fast-forward-only pull. Raises on any non-fast-forward situation
    (diverged history, merge needed) rather than silently creating a merge
    commit in what's meant to be a simple tool install directory.
    """
    r = _run(["git", "pull", "--ff-only", remote, branch], root, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"git pull --ff-only failed: {r.stderr.strip() or r.stdout.strip()}")


def sync_dependencies(root: Path, timeout: int = 120) -> None:
    """Re-sync the uv venv against pyproject.toml/uv.lock after a pull."""
    r = _run(["uv", "sync"], root, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"uv sync failed: {r.stderr.strip() or r.stdout.strip()}")


def run_upgrade(root: Path | None = None) -> tuple[UpgradeStatus, list[str]]:
    """Check, pull if behind, and sync dependencies. Returns (status, changelog).

    Raises DirtyWorkingTreeError if there are uncommitted local changes —
    never silently discards local edits. Raises NotAGitRepoError /
    RuntimeError on git/uv failures.
    """
    status = check_for_updates(root)
    if status.dirty:
        raise DirtyWorkingTreeError(
            "Local changes would be overwritten by upgrade:\n  "
            + "\n  ".join(status.dirty_files)
            + "\nCommit, stash, or discard them first."
        )

    if status.up_to_date:
        return status, []

    log = changelog_since(status.repo_root, status.current_commit, status.latest_commit)
    pull(status.repo_root, status.remote_name, status.branch)
    sync_dependencies(status.repo_root)

    new_status = check_for_updates(status.repo_root)
    return new_status, log
