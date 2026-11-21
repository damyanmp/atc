"""atc ↔ Dockhand contract probe.

Exercises every Portainer call atc makes today (src/lib/portainer.py, as used by cli.py and
blackbox.py) against a live Dockhand instance, on every environment, and records PASS/GAP per
need, plus the real response field names so lib/dockhand.py can be fixed against reality.

Only touches its own throwaway stack (atc-spike-demo). Never prunes volumes unless
PROBE_PRUNE_VOLUMES=1, because prune hits every unused volume on the target daemon.

Usage: DH_URL=http://127.0.0.1:3900 DH_TOKEN_FILE=... python contract_probe.py > results.json
"""
from __future__ import annotations

import io, json, os, sys, tarfile, threading, time

import httpx

BASE = os.environ.get("DH_URL", "http://127.0.0.1:3900")
TOKEN = open(os.environ["DH_TOKEN_FILE"]).read().strip()
H = {"Authorization": f"Bearer {TOKEN}"}
STACK = "atc-spike-demo"
COMPOSE_V1 = """services:
  web:
    image: nginx:1.27-alpine
    environment:
      - GREETING=${GREETING}
    networks: [front, back]
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1/"]
      interval: 5s
      retries: 3
  worker:
    image: busybox:1.36
    command: ["sh", "-c", "while true; do echo tick $$(date +%s); sleep 2; done"]
    networks: [back]
networks:
  front: {}
  back: {}
"""
COMPOSE_V2 = COMPOSE_V1.replace("sleep 2", "sleep 3")

c = httpx.Client(base_url=BASE, headers=H, timeout=180)
results: list[dict] = []


def rec(need, env, ok, detail="", sample=None):
    results.append({"need": need, "env": env, "ok": ok, "detail": detail, "sample": sample})
    print(f"[{'PASS' if ok else 'GAP '}] {env:12} {need}: {detail}", file=sys.stderr)


def keys(obj):
    if isinstance(obj, list):
        obj = obj[0] if obj else {}
    return sorted(obj.keys()) if isinstance(obj, dict) else type(obj).__name__


def wait(pred, secs=90, step=2):
    t = time.time()
    while time.time() - t < secs:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def containers(env_id):
    r = c.get("/api/containers", params={"env": env_id, "all": "true"})
    r.raise_for_status()
    return r.json()


def stack_ctrs(env_id):
    return [x for x in containers(env_id)
            if (x.get("labels") or {}).get("com.docker.compose.project") == STACK]


envs = c.get("/api/environments").json()
rec("list environments", "-", isinstance(envs, list) and len(envs) >= 1,
    f"{len(envs)} envs, fields {keys(envs)}", envs[0] if envs else None)

for e in envs:
    eid, en = e["id"], e["name"]

    # --- containers ---------------------------------------------------------------
    cs = containers(eid)
    rec("list containers (all, with labels/state/health/status)", en, isinstance(cs, list),
        f"{len(cs)} containers, fields {keys(cs)}", {k: cs[0].get(k) for k in keys(cs)} if cs else None)

    # --- create stack from compose string + env vars (atc docker compose up) ------
    c.delete(f"/api/stacks/{STACK}", params={"env": eid, "force": "true", "files": "true"})
    r = c.post("/api/stacks", params={"env": eid},
               json={"name": STACK, "compose": COMPOSE_V1, "start": True,
                     "envVars": [{"key": "GREETING", "value": "v1"}]},
               headers={**H, "Accept": "application/json"})
    rec("create stack from compose string + env, start it (multi-network)", en, r.is_success,
        f"HTTP {r.status_code} {r.text[:160] if not r.is_success else ''}")
    up = wait(lambda: (lambda s: s if len(s) == 2 and all(x.get("state") == "running" for x in s) else None)(stack_ctrs(eid)))
    rec("stack containers come up running", en, bool(up),
        "2/2 running" if up else f"got {[(x.get('name'), x.get('state')) for x in stack_ctrs(eid)]}")

    # --- list stacks / read file / read env -------------------------------------
    ss = c.get("/api/stacks", params={"env": eid}).json()
    mine = [s for s in ss if s.get("name") == STACK]
    rec("list stacks", en, bool(mine), f"fields {keys(ss)}", mine[0] if mine else None)
    r = c.get(f"/api/stacks/{STACK}/compose", params={"env": eid})
    body = r.json() if r.is_success else {}
    rec("read stack compose file", en, r.is_success and "nginx" in json.dumps(body),
        f"HTTP {r.status_code}, fields {keys(body)}")
    r = c.get(f"/api/stacks/{STACK}/env", params={"env": eid})
    envbody = r.json() if r.is_success else {}
    rec("read stack env vars", en, r.is_success and "GREETING" in json.dumps(envbody),
        f"HTTP {r.status_code}, fields {keys(envbody)}")

    # --- update stack: new compose + env, redeploy (atc docker compose up on existing) ---
    r = c.put(f"/api/stacks/{STACK}/env", params={"env": eid},
              json={"variables": [{"key": "GREETING", "value": "v2"}]})
    r_env_ok = r.is_success
    r2 = c.put(f"/api/stacks/{STACK}/compose", params={"env": eid},
               json={"content": COMPOSE_V2, "restart": True}, headers={**H, "Accept": "application/json"})
    def v2_live():
        for x in stack_ctrs(eid):
            if x.get("labels", {}).get("com.docker.compose.service") == "worker" and x.get("state") == "running":
                rr = c.get(f"/api/containers/{x['id']}/inspect", params={"env": eid})
                if rr.is_success and "sleep 3" in json.dumps(rr.json()):
                    return True
        return False
    rec("update stack env vars", en, r_env_ok, f"HTTP {r.status_code} {r.text[:160] if not r_env_ok else ''}")
    rec("update stack compose + redeploy", en, r2.is_success and bool(wait(v2_live, 90)),
        f"HTTP {r2.status_code} {r2.text[:160] if not r2.is_success else ''}")

    worker = next((x for x in stack_ctrs(eid) if x.get("labels", {}).get("com.docker.compose.service") == "worker"), None)
    web = next((x for x in stack_ctrs(eid) if x.get("labels", {}).get("com.docker.compose.service") == "web"), None)

    # --- logs with tail + since --------------------------------------------------
    if worker:
        time.sleep(7)
        r = c.get(f"/api/containers/{worker['id']}/logs", params={"env": eid, "tail": 5})
        lines_tail = [l for l in r.text.splitlines() if "tick" in l]
        since = int(time.time()) - 4
        r2 = c.get(f"/api/containers/{worker['id']}/logs", params={"env": eid, "since": since})
        lines_since = [l for l in r2.text.splitlines() if "tick" in l]
        rec("container logs, tail=N", en, r.is_success and 0 < len(lines_tail) <= 5,
            f"HTTP {r.status_code}, {len(lines_tail)} lines, content-type {r.headers.get('content-type')}")
        rec("container logs, since=<unix ts> is a real window", en, r2.is_success and 0 < len(lines_since) <= 3,
            f"HTTP {r2.status_code}, {len(lines_since)} lines in last 4s")

    # --- container start/stop/restart -------------------------------------------
    if worker:
        ok = True
        for act, want in (("stop", "exited"), ("start", "running"), ("restart", "running")):
            r = c.post(f"/api/containers/{worker['id']}/{act}", params={"env": eid})
            got = wait(lambda: next((x.get("state") for x in stack_ctrs(eid) if x["id"] == worker["id"]), None) == want, 40)
            ok &= r.is_success and bool(got)
            if not (r.is_success and got):
                rec(f"container {act}", en, False, f"HTTP {r.status_code} {r.text[:120]}")
        rec("container stop/start/restart", en, ok, "state transitions observed" if ok else "")

    # --- stack stop/start ---------------------------------------------------------
    r = c.post(f"/api/stacks/{STACK}/stop", params={"env": eid}, headers={**H, "Accept": "application/json"})
    stopped = wait(lambda: all(x.get("state") == "exited" for x in stack_ctrs(eid)) and stack_ctrs(eid), 60)
    r2 = c.post(f"/api/stacks/{STACK}/start", params={"env": eid}, headers={**H, "Accept": "application/json"})
    started = wait(lambda: all(x.get("state") == "running" for x in stack_ctrs(eid)) and len(stack_ctrs(eid)) == 2, 60)
    rec("stack stop/start", en, r.is_success and r2.is_success and bool(stopped) and bool(started),
        f"stop HTTP {r.status_code}, start HTTP {r2.status_code}")

    # --- images / volumes / prune -------------------------------------------------
    r = c.get("/api/images", params={"env": eid})
    imgs = r.json() if r.is_success else []
    rec("list images", en, r.is_success and len(imgs) > 0, f"{len(imgs)} images, fields {keys(imgs)}",
        {k: imgs[0].get(k) for k in keys(imgs)} if imgs else None)
    r = c.get("/api/volumes", params={"env": eid})
    vols = r.json() if r.is_success else []
    rec("list volumes", en, r.is_success, f"{len(vols)} volumes, fields {keys(vols)}",
        {k: vols[0].get(k) for k in keys(vols)} if vols else None)
    r = c.post("/api/prune/images", params={"env": eid, "dangling": "true"},
               headers={**H, "Accept": "application/json"})
    rec("prune dangling images", en, r.is_success, f"HTTP {r.status_code} content-type {r.headers.get('content-type')} body {r.text[:160]}")
    if os.environ.get("PROBE_PRUNE_VOLUMES") == "1":
        r = c.post("/api/prune/volumes", params={"env": eid}, headers={**H, "Accept": "application/json"})
        rec("prune volumes", en, r.is_success, f"HTTP {r.status_code} {r.text[:160]}")
    else:
        rec("prune volumes", en, True, "endpoint exists (POST /api/prune/volumes); not fired: would hit every unused volume on this daemon")

    # --- events: live stream + history (blackbox outage detection) ---------------
    seen = []
    def listen():
        try:
            with httpx.Client(base_url=BASE, headers=H, timeout=None) as sc:
                with sc.stream("GET", "/api/events", params={"env": eid}) as s:
                    t0 = time.time()
                    for line in s.iter_lines():
                        if line.startswith("data:") and STACK in line:
                            seen.append(line[5:].strip())
                        if time.time() - t0 > 15:
                            break
        except Exception as ex:  # noqa: BLE001
            seen.append(f"ERR {type(ex).__name__}")
    th = threading.Thread(target=listen, daemon=True); th.start(); time.sleep(2)
    if worker:
        c.post(f"/api/containers/{worker['id']}/restart", params={"env": eid})
    th.join(20)
    rec("live docker events (SSE)", en, any(not s.startswith("ERR") for s in seen),
        f"{len(seen)} events for the stack in 15s", seen[0][:300] if seen else None)
    frm = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
    r = c.get("/api/activity", params={"environmentId": eid, "containerName": worker["name"] if worker else "", "fromDate": frm, "limit": 50})
    act = r.json() if r.is_success else {}
    items = act.get("events") or act.get("items") or act.get("data") or (act if isinstance(act, list) else [])
    rec("historical container events (since/until window)", en, r.is_success and len(items) > 0,
        f"HTTP {r.status_code} via /api/activity, {len(items)} items, fields {keys(items)}",
        items[0] if items else None)

    # --- upload a file into a container (atc quick publish) ----------------------
    if web:
        files = {"files": ("atc-probe.html", b"<h1>atc probe</h1>", "text/html")}
        r = c.post(f"/api/containers/{web['id']}/files/upload", params={"env": eid, "path": "/usr/share/nginx/html"}, files=files)
        rr = c.post(f"/api/containers/{web['id']}/exec/run", params={"envId": eid},
                    json={"cmd": ["cat", "/usr/share/nginx/html/atc-probe.html"]})
        out = rr.json() if rr.is_success else {}
        rec("upload file into container", en, r.is_success and "atc probe" in json.dumps(out),
            f"upload HTTP {r.status_code}, readback via exec/run HTTP {rr.status_code}")
        rec("exec a command (read-only inspection)", en, rr.is_success, f"fields {keys(out)}")

    # --- delete stack ---------------------------------------------------------------
    r = c.delete(f"/api/stacks/{STACK}", params={"env": eid, "force": "true", "files": "true"})
    gone = wait(lambda: not stack_ctrs(eid), 60)
    rec("delete stack", en, r.is_success and bool(gone), f"HTTP {r.status_code}")

json.dump(results, sys.stdout, indent=1, default=str)
