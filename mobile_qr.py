#!/usr/bin/env python3
"""
Generate a QR code for importing ATC config into the Android mobile app.

Secrets are resolved automatically from your macOS Keychain — no copy-pasting tokens.

Usage (from infrastructure/cli/):
    python mobile_qr.py                          # terminal QR, all services
    python mobile_qr.py --save ~/Desktop/qr.png  # also save as PNG (requires Pillow)
    python mobile_qr.py --only proxmox           # single service
    python mobile_qr.py --only proxmox,portainer # specific subset
    python mobile_qr.py --print-yaml             # show the generated YAML

Then in the Android app: Settings → Scan QR Code.

Install deps (first time only):
    uv sync
    uv add "qrcode[pil]"   # [pil] is optional, needed only for --save PNG
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running directly from the CLI root directory
sys.path.insert(0, str(Path(__file__).parent))

try:
    import qrcode
    import qrcode.constants
except ImportError:
    print("qrcode not installed.  Run:  uv add qrcode")
    sys.exit(1)

import yaml
from src.config import load_config


# ── Config mapping ────────────────────────────────────────────────────────────

def build_mobile_yaml(cfg, only: set[str] | None) -> str:
    """
    Convert the resolved CLI Config into the YAML format expected by AtcConfig
    on Android.  Only includes services the app currently supports.

    CLI key      → Android key
    proxmox      → proxmox           (same structure)
    pihole       → pihole_instances  (list, key rename)
    plex         → plex              (same)
    tautulli     → tautulli          (same)
    radarr       → radarr            (same)
    sonarr       → sonarr            (same)
    seerr        → seerr             (same)
    portainer    → portainer         (same)
    beszel       → beszel            (same)
    """
    want = only  # None = include everything supported

    mobile: dict = {}

    # Proxmox
    if _want("proxmox", want) and cfg.proxmox and cfg.proxmox.host:
        mobile["proxmox"] = {
            "host":        cfg.proxmox.host,
            "port":        cfg.proxmox.port,
            "user":        cfg.proxmox.user,
            "token_name":  cfg.proxmox.token_name,
            "token_value": cfg.proxmox.token_value,
            "verify_ssl":  cfg.proxmox.verify_ssl,
        }

    # Pi-hole  (CLI key = "pihole", Android key = "pihole_instances")
    if _want("pihole", want) and cfg.pihole_instances:
        mobile["pihole_instances"] = [
            {
                "name":     p.name,
                "host":     p.host,
                "port":     p.port,
                "password": p.password,
                "primary":  p.primary,
            }
            for p in cfg.pihole_instances
        ]

    # Plex
    if _want("plex", want) and cfg.plex:
        mobile["plex"] = {
            "host":  cfg.plex.host,
            "port":  cfg.plex.port,
            "token": cfg.plex.token,
        }

    # Tautulli
    if _want("tautulli", want) and cfg.tautulli:
        mobile["tautulli"] = {
            "host":    cfg.tautulli.host,
            "port":    cfg.tautulli.port,
            "api_key": cfg.tautulli.api_key,
        }

    # Radarr
    if _want("radarr", want) and cfg.radarr:
        mobile["radarr"] = {
            "host":      cfg.radarr.host,
            "port":      cfg.radarr.port,
            "https":     cfg.radarr.https,
            "api_key":   cfg.radarr.api_key,
            "base_path": cfg.radarr.base_path,
        }

    # Sonarr
    if _want("sonarr", want) and cfg.sonarr:
        mobile["sonarr"] = {
            "host":      cfg.sonarr.host,
            "port":      cfg.sonarr.port,
            "https":     cfg.sonarr.https,
            "api_key":   cfg.sonarr.api_key,
            "base_path": cfg.sonarr.base_path,
        }

    # Seerr (Overseerr / Jellyseerr)
    if _want("seerr", want) and cfg.seerr:
        mobile["seerr"] = {
            "host":    cfg.seerr.host,
            "port":    cfg.seerr.port,
            "https":   cfg.seerr.https,
            "api_key": cfg.seerr.api_key,
        }

    # Portainer
    if _want("portainer", want) and cfg.portainer:
        mobile["portainer"] = {
            "host":    cfg.portainer.host,
            "port":    cfg.portainer.port,
            "api_key": cfg.portainer.api_key,
        }

    # Beszel
    if _want("beszel", want) and cfg.beszel:
        mobile["beszel"] = {
            "host":    cfg.beszel.host,
            "port":    cfg.beszel.port,
            "api_key": cfg.beszel.api_key,
        }

    if not mobile:
        print("No matching services found in config.yaml for the requested filter.")
        sys.exit(1)

    return yaml.dump(mobile, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _want(service: str, only: set[str] | None) -> bool:
    return only is None or service in only


# ── QR generation ─────────────────────────────────────────────────────────────

QR_CAPACITY_BYTES = 2953   # max for L error-correction, binary mode


def make_qr(data: str) -> "qrcode.QRCode":
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=2,
    )
    qr.add_data(data.encode("utf-8"))
    qr.make(fit=True)
    return qr


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", type=Path,
        help="Path to config.yaml (default: infrastructure/cli/config.yaml)",
    )
    parser.add_argument(
        "--only", metavar="SERVICE[,SERVICE]",
        help="Comma-separated services to include: proxmox,pihole,plex,tautulli,radarr,sonarr,seerr,portainer,beszel",
    )
    parser.add_argument(
        "--save", type=Path, metavar="FILE.png",
        help="Save QR as a PNG image (requires: uv add 'qrcode[pil]')",
    )
    parser.add_argument(
        "--print-yaml", action="store_true",
        help="Print the generated YAML to stdout",
    )
    args = parser.parse_args()

    only = {s.strip().lower() for s in args.only.split(",")} if args.only else None

    print("Resolving config (keychain secrets will be fetched automatically)…")
    cfg = load_config(args.config)

    yaml_content = build_mobile_yaml(cfg, only)
    byte_size = len(yaml_content.encode("utf-8"))

    if byte_size > QR_CAPACITY_BYTES:
        print(f"\n✗  Config is {byte_size} bytes — exceeds QR capacity ({QR_CAPACITY_BYTES} bytes).")
        print("   Use --only to include only the services you need right now.")
        sys.exit(1)

    if args.print_yaml:
        print("\n── Generated YAML ──────────────────────────────────────")
        print(yaml_content)
        print("────────────────────────────────────────────────────────\n")

    qr = make_qr(yaml_content)

    service_names = _summarise(yaml_content)
    print(f"\n── ATC Mobile Config QR  ({byte_size} bytes · {service_names}) ──\n")

    # invert=True looks correct on dark terminals (most dev setups)
    qr.print_ascii(invert=True)

    print("\n  Scan with ATC app → Settings → Scan QR Code")
    print("  Tip: if the scan fails, zoom out in your terminal or use --save to get a PNG.\n")

    if args.save:
        _save_png(qr, args.save)


def _summarise(yaml_content: str) -> str:
    import re
    top_keys = re.findall(r"^(\w+):", yaml_content, re.MULTILINE)
    labels = {"proxmox": "Proxmox", "pihole_instances": "Pi-hole",
              "plex": "Plex", "tautulli": "Tautulli",
              "radarr": "Radarr", "sonarr": "Sonarr", "seerr": "Seerr",
              "portainer": "Portainer", "beszel": "Beszel"}
    return " · ".join(labels.get(k, k) for k in top_keys if k in labels)


def _save_png(qr: "qrcode.QRCode", path: Path) -> None:
    try:
        from PIL import Image  # noqa: F401
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(path)
        print(f"  PNG saved to {path}")
    except ImportError:
        print("  Pillow not installed — cannot save PNG.")
        print("  Run: uv add 'qrcode[pil]'")


if __name__ == "__main__":
    main()
