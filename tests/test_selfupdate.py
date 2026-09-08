"""Tests for lib/selfupdate.py — uses real scratch git repos, not mocks.

Real git operations against temp dirs are cheap and far more trustworthy
than mocking subprocess.run for something this failure-sensitive (a self
-update command that pulls and syncs deps on a live homelab tool).
"""
from __future__ import annotations

import subprocess

import pytest

from lib.selfupdate import (
    DirtyWorkingTreeError,
    NotAGitRepoError,
    changelog_since,
    check_for_updates,
    commit_hash,
    current_branch,
    dirty_files,
    is_git_repo,
    remote_name_for_branch,
    remote_url,
    run_upgrade,
)


def _git(args, cwd):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


def _init_repo(path, branch="main"):
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", branch], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    return path


def _commit(path, filename, content, message):
    (path / filename).write_text(content)
    _git(["add", "-A"], path)
    _git(["commit", "-m", message], path)


@pytest.fixture
def upstream_and_clone(tmp_path):
    """A bare-ish 'remote' repo plus a clone of it that acts as the atc install dir."""
    remote = tmp_path / "remote"
    _init_repo(remote)
    _commit(remote, "file.txt", "v1", "initial commit")

    clone = tmp_path / "clone"
    _git(["clone", str(remote), str(clone)], tmp_path)
    _git(["config", "user.email", "test@example.com"], clone)
    _git(["config", "user.name", "Test"], clone)
    return remote, clone


# ── basic git helpers ────────────────────────────────────────────────────────

class TestGitHelpers:
    def test_is_git_repo_true(self, upstream_and_clone):
        _, clone = upstream_and_clone
        assert is_git_repo(clone)

    def test_is_git_repo_false(self, tmp_path):
        plain_dir = tmp_path / "not_a_repo"
        plain_dir.mkdir()
        assert not is_git_repo(plain_dir)

    def test_current_branch(self, upstream_and_clone):
        _, clone = upstream_and_clone
        assert current_branch(clone) == "main"

    def test_remote_name_for_branch_defaults_origin(self, upstream_and_clone):
        _, clone = upstream_and_clone
        assert remote_name_for_branch(clone, "main") == "origin"

    def test_remote_url_matches_upstream(self, upstream_and_clone):
        remote, clone = upstream_and_clone
        assert remote_url(clone, "origin") == str(remote)

    def test_dirty_files_empty_on_clean_clone(self, upstream_and_clone):
        _, clone = upstream_and_clone
        assert dirty_files(clone) == []

    def test_dirty_files_detects_untracked(self, upstream_and_clone):
        _, clone = upstream_and_clone
        (clone / "scratch.txt").write_text("oops")
        assert "scratch.txt" in dirty_files(clone)

    def test_dirty_files_detects_modified(self, upstream_and_clone):
        _, clone = upstream_and_clone
        (clone / "file.txt").write_text("modified")
        assert "file.txt" in dirty_files(clone)

    def test_commit_hash_matches_head(self, upstream_and_clone):
        _, clone = upstream_and_clone
        h = commit_hash(clone)
        assert len(h) >= 7  # short hash


# ── check_for_updates ────────────────────────────────────────────────────────

class TestCheckForUpdates:
    def test_up_to_date_when_no_new_commits(self, upstream_and_clone):
        _, clone = upstream_and_clone
        status = check_for_updates(clone)
        assert status.up_to_date
        assert status.commits_behind == 0
        assert not status.dirty

    def test_detects_commits_behind(self, upstream_and_clone):
        remote, clone = upstream_and_clone
        _commit(remote, "file.txt", "v2", "second commit")
        _commit(remote, "file.txt", "v3", "third commit")

        status = check_for_updates(clone)
        assert not status.up_to_date
        assert status.commits_behind == 2
        assert status.current_commit != status.latest_commit

    def test_reports_correct_branch_and_remote(self, upstream_and_clone):
        _, clone = upstream_and_clone
        status = check_for_updates(clone)
        assert status.branch == "main"
        assert status.remote_name == "origin"

    def test_flags_dirty_working_tree(self, upstream_and_clone):
        _, clone = upstream_and_clone
        (clone / "file.txt").write_text("locally edited")
        status = check_for_updates(clone)
        assert status.dirty
        assert "file.txt" in status.dirty_files

    def test_not_a_git_repo_raises(self, tmp_path):
        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()
        with pytest.raises(NotAGitRepoError):
            check_for_updates(plain_dir)

    def test_remote_url_override(self, upstream_and_clone):
        _, clone = upstream_and_clone
        status = check_for_updates(clone, remote_url_override="https://fork.example/atc.git")
        assert status.remote_url == "https://fork.example/atc.git"


# ── changelog_since ──────────────────────────────────────────────────────────

class TestChangelogSince:
    def test_lists_new_commits(self, upstream_and_clone):
        remote, clone = upstream_and_clone
        _commit(remote, "file.txt", "v2", "add feature X")
        _commit(remote, "file.txt", "v3", "fix bug Y")

        _git(["fetch", "origin"], clone)
        log = changelog_since(clone, "HEAD", "origin/main")
        assert len(log) == 2
        assert any("add feature X" in line for line in log)
        assert any("fix bug Y" in line for line in log)

    def test_empty_when_no_new_commits(self, upstream_and_clone):
        _, clone = upstream_and_clone
        _git(["fetch", "origin"], clone)
        log = changelog_since(clone, "HEAD", "origin/main")
        assert log == []


# ── run_upgrade (full flow) ──────────────────────────────────────────────────

class TestRunUpgrade:
    def test_pulls_and_updates_clean_repo(self, upstream_and_clone, monkeypatch):
        remote, clone = upstream_and_clone
        _commit(remote, "file.txt", "v2", "new commit")

        # sync_dependencies calls `uv sync` — stub it out, not testing uv here
        import lib.selfupdate as su
        monkeypatch.setattr(su, "sync_dependencies", lambda root, timeout=120: None)

        status, log = run_upgrade(clone)
        assert status.up_to_date
        assert status.current_commit == status.latest_commit
        assert len(log) == 1
        assert "new commit" in log[0]
        assert (clone / "file.txt").read_text() == "v2"

    def test_refuses_when_dirty(self, upstream_and_clone):
        remote, clone = upstream_and_clone
        _commit(remote, "file.txt", "v2", "new commit")
        (clone / "file.txt").write_text("local edit")

        with pytest.raises(DirtyWorkingTreeError):
            run_upgrade(clone)

    def test_noop_when_already_up_to_date(self, upstream_and_clone, monkeypatch):
        _, clone = upstream_and_clone
        import lib.selfupdate as su
        monkeypatch.setattr(su, "sync_dependencies", lambda root, timeout=120: None)

        status, log = run_upgrade(clone)
        assert status.up_to_date
        assert log == []
