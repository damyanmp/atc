"""End-to-end run of `atc init dockhand` against the sandbox, with scripted answers.

The real keychain is never touched: config.secret_set / keyring lookups are swapped for an
in-memory dict, and config.yaml goes to a temp file. The edge-mode step starts a real
Hawser edge container when the wizard says "Press Enter once the agent is running".

  DH_TOKEN_FILE, HW_TOKEN_FILE as for the other probes. Prints the transcript.
"""
from __future__ import annotations

import os, subprocess, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import keyring  # noqa: E402

import cli  # noqa: E402
import config  # noqa: E402
from lib import dockhand as dh  # noqa: E402

TOKEN = open(os.environ["DH_TOKEN_FILE"]).read().strip()
HW = open(os.environ["HW_TOKEN_FILE"]).read().split("=", 1)[1].strip()
STORE: dict[str, str] = {}
config.secret_set = cli.secret_set = lambda k, v, local_only=None: STORE.__setitem__(k, v)  # type: ignore
keyring.get_password = lambda svc, k: STORE.get(k)  # type: ignore

captured: dict = {}
_orig_cmd = dh.hawser_run_command
def _cap(mode, token, connect_url="", version="latest"):
    captured.update(mode=mode, token=token)
    return _orig_cmd(mode, token, connect_url, version)
dh.hawser_run_command = _cap  # type: ignore

answers: list = []
transcript: list[str] = []


def _ask(kind):
    def f(prompt, *a, **kw):
        prompt = str(prompt).strip()
        if "Press Enter once the agent is running" in prompt and captured.get("mode") == "hawser-edge":
            subprocess.run(["docker", "rm", "-f", "hw-edge-spike"], capture_output=True)
            subprocess.run(["docker", "run", "-d", "--name", "hw-edge-spike", "--network", "dh-spike",
                            "-e", "DOCKHAND_SERVER_URL=ws://dh-spike:3000/api/hawser/connect",
                            "-e", f"TOKEN={captured['token']}", "-v", "/var/run/docker.sock:/var/run/docker.sock",
                            "--tmpfs", "/tmp", "--label", "dockhand.update=false", "ghcr.io/finsys/hawser:latest"],
                           check=True, capture_output=True)
            transcript.append(f"? {prompt} -> (started edge agent)")
            return ""
        if not answers:
            raise SystemExit(f"ran out of scripted answers at: {prompt}")
        ans = answers.pop(0)
        shown = "***" if ("token" in prompt.lower()) else ans
        transcript.append(f"? {prompt} -> {shown}")
        return ans
    return f


cli.Prompt.ask = staticmethod(_ask("prompt"))  # type: ignore
cli.Confirm.ask = staticmethod(_ask("confirm"))  # type: ignore

cfg_path = Path(tempfile.mkdtemp()) / "config.yaml"

# Run 1: configure Dockhand, add a standard agent, an edge agent, and a direct host that fails.
answers[:] = [
    "127.0.0.1", "3900", False, TOKEN, False,          # section fields
    True,                                               # register a host now?
    "1", "t-std", "hw-spike", "2376", False, True, HW,  # hawser-standard, agent already running
    True,                                               # add another
    "2", "t-edge",                                      # hawser-edge
    True,                                               # add another
    "4", "t-direct", "hw-spike", "2399", False, "3",    # direct, test fails -> skip
    False,                                              # add another
]
cli.cmd_init(section="dockhand", config_path_opt=cfg_path)
print("\n--- run 1 transcript ---"); print("\n".join(transcript)); transcript.clear()
print("config.yaml:\n" + cfg_path.read_text())
print("keychain keys written:", sorted(STORE))

# Run 2: already configured -> add hosts only; duplicate name is refused.
answers[:] = ["1", True, "3", "t-std", "t-sock", "/var/run/docker.sock", False]
cli.cmd_init(section="dockhand", config_path_opt=cfg_path)
print("\n--- run 2 transcript ---"); print("\n".join(transcript))

c = config.DockhandConfig(host="127.0.0.1", port=3900, api_token=TOKEN)
print("\nDockhand environments now:")
for e in dh.fetch_environments(c):
    ok, msg = dh.test_environment(c, e.id)
    print(f"  {e.id} {e.name:12} {e.connection_type:16} test={'ok' if ok else msg}")
