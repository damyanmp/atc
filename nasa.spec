# nasa.spec — PyInstaller build spec for the nasa CLI binary
# Build with:  uv run --dev pyinstaller nasa.spec

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

SRC = Path("src")

a = Analysis(
    [str(SRC / "nasa.py")],
    pathex=[str(SRC), str(SRC / "lib")],
    binaries=[],
    datas=[
        *collect_data_files("textual"),
        *collect_data_files("rich"),
    ],
    hiddenimports=[
        *collect_submodules("textual"),
        *collect_submodules("rich"),
        *collect_submodules("httpx"),
        "typer",
        "click",
        "cryptography",
        "lib.horizons",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "textual_dev"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="nasa",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
