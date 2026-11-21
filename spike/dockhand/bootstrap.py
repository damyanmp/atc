"""Bootstrap the throwaway Dockhand sandbox: admin user, auth on, API token, two environments.

Secrets (admin password, API token) are written to 0600 files under SPIKE_DIR and never printed.
"""
import http.cookiejar, json, os, secrets, sys, urllib.error, urllib.request

BASE = os.environ.get("DH_URL", "http://127.0.0.1:3900")
D = os.environ["SPIKE_DIR"]
os.umask(0o077)
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with op.open(req, timeout=30) as r:
            txt = r.read().decode()
            return r.status, (json.loads(txt) if txt.strip().startswith(("{", "[")) else txt)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


pw_file = f"{D}/admin.pw"
if not os.path.exists(pw_file):
    open(pw_file, "w").write(secrets.token_urlsafe(24))
pw = open(pw_file).read().strip()

st, _ = call("POST", "/api/users", {"username": "atc-spike", "password": pw})
print("create user:", st)
st, r = call("PUT", "/api/auth/settings", {"authEnabled": True})
print("enable auth:", st)
st, _ = call("GET", "/api/environments")
print("unauthenticated GET /api/environments after auth on:", st, "(expect 401)")
st, _ = call("POST", "/api/auth/login", {"username": "atc-spike", "password": pw})
print("login:", st)
st, r = call("POST", "/api/auth/tokens", {"name": "atc-spike", "password": pw})
tok = (r.get("token") or r.get("value") or r.get("plaintext")) if isinstance(r, dict) else None
print("create token:", st, "keys:", sorted(r.keys()) if isinstance(r, dict) else r)
if tok:
    open(f"{D}/token", "w").write(tok)
    print("token saved, length", len(tok))
else:
    sys.exit("no token in response")

hw_token = open(f"{D}/hawser.env").read().split("=", 1)[1].strip()
st, envs = call("GET", "/api/environments", token=tok)
print("bearer GET /api/environments:", st, [e.get("name") for e in envs] if isinstance(envs, list) else envs)
names = {e.get("name") for e in envs} if isinstance(envs, list) else set()
if "local" not in names:
    print("add local:", call("POST", "/api/environments",
          {"name": "local", "connectionType": "socket", "socketPath": "/var/run/docker.sock"}, token=tok)[0])
if "hawser-loop" not in names:
    st, r = call("POST", "/api/environments",
                 {"name": "hawser-loop", "connectionType": "hawser-standard", "host": "hw-spike",
                  "port": 2376, "protocol": "http", "hawserToken": hw_token}, token=tok)
    print("add hawser-loop:", st, r if st >= 400 else "")
st, envs = call("GET", "/api/environments", token=tok)
for e in envs:
    print("env", e.get("id"), e.get("name"), e.get("connectionType"))
