"""Tests for lib/dockhand.py — pure dataclass/logic pieces (no network).

Same testing bar as test_portainer.py: covers parsing-independent logic
(needs_attention, started_within, compose_project, infer_compose_stacks).
It does NOT validate real Dockhand API response shapes — see the big
warning at the top of lib/dockhand.py about unverified field names.
"""
from __future__ import annotations

from lib.dockhand import (
    DockhandContainer,
    DockhandImage,
    InferredStack,
    infer_compose_stacks,
)


def _container(**kwargs) -> DockhandContainer:
    defaults = dict(
        id="abc123", name="peppermint-backend", image="ghcr.io/x/peppermint-backend:v1",
        state="running", status="Up 2 hours", health="", env_id=1, env_name="docker-1",
    )
    defaults.update(kwargs)
    return DockhandContainer(**defaults)


# ── DockhandContainer.compose_project ───────────────────────────────────────

class TestComposeProject:
    def test_no_labels_returns_empty(self):
        c = _container(labels=None)
        assert c.compose_project == ""

    def test_labels_default_to_empty_dict(self):
        c = _container()
        assert c.labels == {}

    def test_returns_label_value(self):
        c = _container(labels={"com.docker.compose.project": "peppermint"})
        assert c.compose_project == "peppermint"


# ── DockhandContainer.needs_attention ───────────────────────────────────────

class TestNeedsAttention:
    def test_running_healthy_ok(self):
        c = _container(state="running", health="healthy")
        assert not c.needs_attention

    def test_running_no_healthcheck_ok(self):
        c = _container(state="running", health="")
        assert not c.needs_attention

    def test_exited_needs_attention(self):
        c = _container(state="exited")
        assert c.needs_attention

    def test_dead_needs_attention(self):
        c = _container(state="dead")
        assert c.needs_attention

    def test_restarting_needs_attention(self):
        c = _container(state="restarting")
        assert c.needs_attention

    def test_unhealthy_needs_attention(self):
        c = _container(state="running", health="unhealthy")
        assert c.needs_attention

    def test_starting_healthcheck_needs_attention(self):
        c = _container(state="running", health="starting")
        assert c.needs_attention

    def test_paused_does_not_need_attention(self):
        # paused is intentional (not exited/dead/restarting) — not flagged
        c = _container(state="paused")
        assert not c.needs_attention


# ── DockhandContainer.started_within ────────────────────────────────────────

class TestStartedWithin:
    def test_up_seconds_within_window(self):
        c = _container(status="Up 47 seconds")
        assert c.started_within(60)

    def test_up_seconds_outside_window(self):
        c = _container(status="Up 90 seconds")
        assert not c.started_within(60)

    def test_up_minutes_converted(self):
        c = _container(status="Up 3 minutes")
        assert c.started_within(300)  # 3*60=180 <= 300
        assert not c.started_within(120)

    def test_up_hours_converted(self):
        c = _container(status="Up 2 hours")
        assert c.started_within(3 * 3600)
        assert not c.started_within(3600)

    def test_exited_status_never_matches(self):
        c = _container(status="Exited (1) 5 minutes ago")
        assert not c.started_within(999999)

    def test_case_insensitive_unit(self):
        c = _container(status="Up 1 Hour")
        assert c.started_within(3600)


# ── DockhandImage ────────────────────────────────────────────────────────────

class TestDockhandImage:
    def test_dangling_true_when_no_tags(self):
        img = DockhandImage(id="sha256:abc", tags=[], size=100, created=0, env_id=1, env_name="e")
        assert img.dangling

    def test_dangling_false_with_tags(self):
        img = DockhandImage(id="sha256:abc", tags=["nginx:latest"], size=100, created=0, env_id=1, env_name="e")
        assert not img.dangling

    def test_display_name_uses_first_tag(self):
        img = DockhandImage(id="sha256:abc", tags=["nginx:latest", "nginx:1.28"], size=100, created=0, env_id=1, env_name="e")
        assert img.display_name == "nginx:latest"

    def test_display_name_dangling_shows_short_id(self):
        img = DockhandImage(id="sha256:abcdef0123456789", tags=[], size=100, created=0, env_id=1, env_name="e")
        assert img.display_name == "<none> (sha256:abcde)"


# ── infer_compose_stacks ─────────────────────────────────────────────────────

class TestInferComposeStacks:
    def test_groups_by_project_label(self):
        containers = [
            _container(name="backend", labels={"com.docker.compose.project": "peppermint"}),
            _container(name="frontend", labels={"com.docker.compose.project": "peppermint"}),
            _container(name="db", labels={"com.docker.compose.project": "peppermint"}),
        ]
        stacks = infer_compose_stacks(containers)
        assert len(stacks) == 1
        assert stacks[0].project == "peppermint"
        assert stacks[0].total == 3

    def test_ignores_containers_without_project_label(self):
        containers = [
            _container(name="standalone", labels={}),
            _container(name="backend", labels={"com.docker.compose.project": "peppermint"}),
        ]
        stacks = infer_compose_stacks(containers)
        assert len(stacks) == 1
        assert stacks[0].total == 1

    def test_skips_known_stack_names_case_insensitive(self):
        containers = [
            _container(name="backend", labels={"com.docker.compose.project": "Peppermint"}),
        ]
        stacks = infer_compose_stacks(containers, known_stack_names={"peppermint"})
        assert stacks == []

    def test_separates_by_env(self):
        containers = [
            _container(name="a", env_id=1, env_name="docker-1", labels={"com.docker.compose.project": "monitoring"}),
            _container(name="b", env_id=2, env_name="docker-2", labels={"com.docker.compose.project": "monitoring"}),
        ]
        stacks = infer_compose_stacks(containers)
        assert len(stacks) == 2
        envs = {s.env_id for s in stacks}
        assert envs == {1, 2}

    def test_running_and_total_counts(self):
        stack = InferredStack(
            project="p", env_id=1, env_name="docker-1",
            containers=[
                _container(state="running"),
                _container(state="running"),
                _container(state="exited"),
            ],
        )
        assert stack.running == 2
        assert stack.total == 3
        assert stack.name == "p"

    def test_sorted_by_env_and_project(self):
        containers = [
            _container(env_id=2, env_name="docker-2", labels={"com.docker.compose.project": "zeta"}),
            _container(env_id=1, env_name="docker-1", labels={"com.docker.compose.project": "alpha"}),
        ]
        stacks = infer_compose_stacks(containers)
        assert [s.env_id for s in stacks] == [1, 2]
