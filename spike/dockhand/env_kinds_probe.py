"""Check the env-kind bodies build_dockhand_environment produces against a live Dockhand."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
from config import DockhandConfig  # noqa: E402
from lib import dockhand as dh  # noqa: E402
from lib.config_sections import build_dockhand_environment  # noqa: E402

cfg = DockhandConfig(host="127.0.0.1", port=3900, api_token=open(os.environ["DH_TOKEN_FILE"]).read().strip())
hw = open(os.environ["HW_TOKEN_FILE"]).read().split("=", 1)[1].strip()
cases = [
    ("socket", {"name": "t-socket"}, True),
    ("hawser-standard", {"name": "t-std", "host": "hw-spike", "hawser_token": hw}, True),
    ("hawser-standard", {"name": "t-std-bad", "host": "hw-spike", "hawser_token": "wrong"}, False),
    ("direct", {"name": "t-direct", "host": "hw-spike", "port": 2399}, False),
]
for kind, ans, expect in cases:
    body = build_dockhand_environment(kind, ans)
    ok, msg = dh.test_environment_config(cfg, {k: v for k, v in body.items() if k != "name"})
    print(f"{'OK ' if ok == expect else 'BAD'} {kind:16} {ans['name']:10} -> {ok} {msg[:120]}")
