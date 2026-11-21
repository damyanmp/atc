"""Dump raw response shapes for the fields atc maps (logs body, container/stack/env/activity rows)."""
import json, os, time
import httpx

BASE = os.environ.get("DH_URL", "http://127.0.0.1:3900")
H = {"Authorization": "Bearer " + open(os.environ["DH_TOKEN_FILE"]).read().strip()}
c = httpx.Client(base_url=BASE, headers=H, timeout=120)
EID = int(os.environ.get("ENV_ID", "2"))
S = "atc-spike-shapes"
compose = 'services:\n  w:\n    image: busybox:1.36\n    command: ["sh","-c","i=0; while true; do i=$$((i+1)); echo line $$i; sleep 1; done"]\n'
c.delete(f"/api/stacks/{S}", params={"env": EID, "force": "true", "files": "true"})
print("create", c.post("/api/stacks", params={"env": EID}, json={"name": S, "compose": compose, "start": True},
                       headers={**H, "Accept": "application/json"}).status_code)
time.sleep(12)
ctr = next(x for x in c.get("/api/containers", params={"env": EID, "all": "true"}).json()
           if x.get("labels", {}).get("com.docker.compose.project") == S)
print("container row:", json.dumps({k: ctr[k] for k in ctr if k not in ("labels", "mounts", "networks", "ports")}, default=str)[:900])
r = c.get(f"/api/containers/{ctr['id']}/logs", params={"env": EID, "tail": 5})
print("logs tail=5 ->", r.headers.get("content-type"), "type", type(r.json()).__name__, "keys", list(r.json().keys()) if isinstance(r.json(), dict) else "")
body = r.json()
txt = body.get("logs") if isinstance(body, dict) else body
print("  lines:", [l for l in str(txt).splitlines() if l.strip()][-6:])
since = int(time.time()) - 3
r = c.get(f"/api/containers/{ctr['id']}/logs", params={"env": EID, "since": since})
txt = r.json().get("logs") if isinstance(r.json(), dict) else r.json()
print("logs since=now-3s ->", len([l for l in str(txt).splitlines() if l.strip()]), "lines (expect ~3)")
st = next(s for s in c.get("/api/stacks", params={"env": EID}).json() if s["name"] == S)
print("stack row:", json.dumps({k: st[k] for k in st if k != "containerDetails"}, default=str)[:600])
c.post(f"/api/containers/{ctr['id']}/restart", params={"env": EID})
time.sleep(3)
frm = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))
a = c.get("/api/activity", params={"environmentId": EID, "containerName": ctr["name"], "fromDate": frm, "limit": 10}).json()
print("activity top-level:", list(a.keys()) if isinstance(a, dict) else type(a).__name__)
items = a.get("events", a) if isinstance(a, dict) else a
print("activity actions:", [i.get("action") for i in items][:10])
print("activity row:", json.dumps({k: items[0][k] for k in items[0] if k != "actorAttributes"}, default=str)[:500])
print("delete", c.delete(f"/api/stacks/{S}", params={"env": EID, "force": "true", "files": "true"}).status_code)
