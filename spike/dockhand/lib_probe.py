"""Drive src/lib/dockhand.py the way each atc docker command drives lib/portainer.py.

One row per atc feature, per Dockhand environment. Touches only its own stack
(atc-lib-probe). Prints a markdown table to stdout; details to stderr.

  DH_URL host/port via DH_HOST/DH_PORT (default 127.0.0.1:3900), token from DH_TOKEN_FILE.
"""
from __future__ import annotations

import io, os, sys, tarfile, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from config import DockhandConfig  # noqa: E402
from lib import dockhand as dh  # noqa: E402
from lib.portainer import outages_from_events  # noqa: E402

cfg = DockhandConfig(host=os.environ.get("DH_HOST", "127.0.0.1"), port=int(os.environ.get("DH_PORT", "3900")),
                     api_token=open(os.environ["DH_TOKEN_FILE"]).read().strip())
STACK = "atc-lib-probe"
COMPOSE = """services:
  web:
    image: nginx:1.27-alpine
    environment:
      - GREETING=${GREETING}
    networks: [front, back]
  worker:
    image: busybox:1.36
    command: ["sh", "-c", "while true; do echo tick $$(date +%s); sleep 2; done"]
    networks: [back]
networks: {front: {}, back: {}}
"""
rows: list[tuple[str, str, bool, str]] = []


def check(cmd, env, fn):
    try:
        ok, note = fn()
    except Exception as ex:  # noqa: BLE001
        ok, note = False, f"{type(ex).__name__}: {str(ex)[:160]}"
    rows.append((cmd, env, ok, note))
    print(f"[{'PASS' if ok else 'FAIL'}] {env:12} {cmd}: {note}", file=sys.stderr)


def wait(pred, secs=90):
    t = time.time()
    while time.time() - t < secs:
        if pred():
            return True
        time.sleep(2)
    return False


envs = dh.fetch_environments(cfg)
check("atc docker ls (environments)", "-", lambda: (len(envs) >= 1, ", ".join(f"{e.name}/{e.connection_type}" for e in envs)))

for e in envs:
    ctrs = lambda: [c for c in dh.fetch_containers(cfg, e.id, e.name) if c.compose_project == STACK]  # noqa: E731
    try:
        dh.delete_stack(cfg, e.id, STACK)
    except RuntimeError:
        pass

    check("atc docker compose up (new stack, env vars, 2 networks)", e.name, lambda: (
        dh.create_stack(cfg, e.id, STACK, COMPOSE, [{"name": "GREETING", "value": "v1"}]) == STACK
        and wait(lambda: len(ctrs()) == 2 and all(c.state == "running" for c in ctrs())), "2/2 running"))

    def ls():
        cs = dh.fetch_containers(cfg, e.id, e.name)
        mine = [c for c in cs if c.compose_project == STACK]
        return len(mine) == 2 and all(c.status.startswith("Up") for c in mine), f"{len(cs)} containers; status '{mine[0].status}'"
    check("atc docker ls (containers, labels, status)", e.name, ls)

    def stacks():
        ss = dh.fetch_stacks(cfg, [e])
        managed = {s.name for s in ss}
        inferred = dh.infer_compose_stacks(dh.fetch_containers(cfg, e.id, e.name), managed)
        mine = [s for s in ss if s.name == STACK]
        return bool(mine) and mine[0].status == "running", f"{len(ss)} managed ({mine[0].source_type}), {len(inferred)} inferred-only"
    check("atc docker stacks", e.name, stacks)

    def update():
        env_before = dh.fetch_stack_env(cfg, e.id, STACK)
        dh.update_stack(cfg, e.id, STACK, dh.fetch_stack_file(cfg, e.id, STACK).replace("sleep 2", "sleep 3"))
        env_after = dh.fetch_stack_env(cfg, e.id, STACK)
        dh.update_stack(cfg, e.id, STACK, dh.fetch_stack_file(cfg, e.id, STACK), [{"name": "GREETING", "value": "v2"}])
        got = dh.fetch_stack_env(cfg, e.id, STACK)
        live = wait(lambda: len(ctrs()) == 2 and all(c.state == "running" for c in ctrs()), 60)
        import httpx
        web = next(c for c in ctrs() if c.labels.get("com.docker.compose.service") == "web")
        with httpx.Client(base_url=cfg.base_url, headers=dh._headers(cfg)) as cl:
            seen = cl.post(f"/api/containers/{web.id}/exec/run", params={"envId": e.id},
                           json={"cmd": ["printenv", "GREETING"]}).json().get("stdout", "").strip()
        return (env_before == env_after and got == [{"name": "GREETING", "value": "v2"}] and live and seen == "v2",
                f"compose-only update kept env {env_after}; env update -> {got}; container sees GREETING={seen}")
    check("atc docker compose up (existing stack: file + env)", e.name, update)

    def guard():
        try:
            dh.update_stack(cfg, e.id, STACK, "x", [])
            return False, "empty env list accepted"
        except ValueError:
            return True, "env_vars=[] refused without force_clear_env"
    check("update_stack env-wipe guard", e.name, guard)

    def logs():
        w = next(c for c in ctrs() if c.labels.get("com.docker.compose.service") == "worker")
        time.sleep(8)
        tail = [l for l in dh.fetch_container_logs(cfg, e.id, w.id, since_seconds=0, tail=3).splitlines() if l]
        win = [l for l in dh.fetch_container_logs(cfg, e.id, w.id, since_seconds=5, tail=500).splitlines() if l]
        return len(tail) == 3 and 1 <= len(win) <= 4, f"tail=3 -> {len(tail)} lines; --since 5s -> {len(win)} lines; e.g. {tail[-1][:48]!r}"
    check("atc docker logs (-n, --since)", e.name, logs)

    def actions():
        w = next(c for c in ctrs() if c.labels.get("com.docker.compose.service") == "worker")
        seq = []
        for act, want in (("stop", "exited"), ("start", "running"), ("restart", "running")):
            dh.container_action(cfg, e.id, w.id, act)
            seq.append(wait(lambda: next(c.state for c in ctrs() if c.id == w.id) == want, 40))
        return all(seq), "stop/start/restart"
    check("atc docker start/stop/restart <container>", e.name, actions)

    def stack_actions():
        dh.stack_action(cfg, e.id, STACK, "stop")
        a = wait(lambda: all(c.state == "exited" for c in ctrs()), 60)
        dh.stack_action(cfg, e.id, STACK, "start")
        b = wait(lambda: all(c.state == "running" for c in ctrs()), 60)
        return a and b, "stop -> exited, start -> running"
    check("atc docker start/stop <stack>", e.name, stack_actions)

    check("atc docker images", e.name, lambda: (lambda im: (len(im) > 0 and any(i.tags for i in im), f"{len(im)} images, {sum(i.dangling for i in im)} dangling"))(dh.fetch_images(cfg, e.id, e.name)))
    check("atc docker volumes", e.name, lambda: (lambda v: (isinstance(v, list), f"{len(v)} volumes"))(dh.fetch_volumes(cfg, e.id, e.name)))
    check("atc docker prune --no-volumes (dangling images)", e.name, lambda: (lambda r: (True, f"deleted {r[0]}, reclaimed {r[1]} B"))(dh.prune_images(cfg, e.id)))

    def quick():
        web = next(c for c in ctrs() if c.labels.get("com.docker.compose.service") == "web")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, data in (("probe-slug/index.html", b"<h1>quick probe</h1>"), ("probe-slug/img/a.svg", b"<svg/>")):
                ti = tarfile.TarInfo(name); ti.size = len(data); tar.addfile(ti, io.BytesIO(data))
        dh.upload_to_container(cfg, e.id, web.id, buf.getvalue(), "/usr/share/nginx/html")
        import httpx
        with httpx.Client(base_url=cfg.base_url, headers=dh._headers(cfg)) as cl:
            out = cl.post(f"/api/containers/{web.id}/exec/run", params={"envId": e.id},
                          json={"cmd": ["sh", "-c", "cat /usr/share/nginx/html/probe-slug/index.html; ls /usr/share/nginx/html/probe-slug/img"]}).json()
        return "quick probe" in out.get("stdout", "") and "a.svg" in out.get("stdout", ""), "nested dir tar landed"
    check("atc quick publish (tar into container)", e.name, quick)

    def outages():
        w = next(c for c in ctrs() if c.labels.get("com.docker.compose.service") == "worker")
        dh.container_action(cfg, e.id, w.id, "stop")
        time.sleep(3)
        dh.container_action(cfg, e.id, w.id, "start")
        time.sleep(3)
        ev = dh.fetch_container_events(cfg, e.id, since_seconds=900)
        out = [o for o in outages_from_events(e.name, ev) if o.container_name == w.name]
        acts = sorted({x["Action"] for x in ev})
        return len(out) >= 1 and set(acts) <= {"start", "stop", "die", "kill", "oom"}, f"{len(ev)} events {acts}; {len(out)} outages for worker"
    check("atc blackbox (container outages)", e.name, outages)

    def bad_compose():
        try:
            dh.create_stack(cfg, e.id, "atc-lib-probe-bad", "services:\n  x:\n    image: [oops\n")
            return False, "broken compose reported as success"
        except RuntimeError as ex:
            return True, f"raised: {str(ex)[:70]}"
        finally:
            try:
                dh.delete_stack(cfg, e.id, "atc-lib-probe-bad")
            except RuntimeError:
                pass
    check("failed deploy surfaces as an error (HTTP 200 + success:false)", e.name, bad_compose)

    check("atc docker compose down (delete stack)", e.name, lambda: (dh.delete_stack(cfg, e.id, STACK) is None and wait(lambda: not ctrs(), 60), "containers gone"))

print("| atc feature | env | result | note |\n|---|---|---|---|")
for cmd, env, ok, note in rows:
    print(f"| {cmd} | {env} | {'PASS' if ok else 'FAIL'} | {note} |")
print(f"\n{sum(r[2] for r in rows)}/{len(rows)} passed", file=sys.stderr)
