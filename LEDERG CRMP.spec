# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
datas = [
    (str(root / "runtime-manifest.json"), "."),
    (str(root / "client_updates.py"), "runtime"),
    (str(root / "radio_sync.py"), "runtime"),
    (str(root / "launcher.py"), "runtime"),
    (str(root / "web_runtime.py"), "runtime"),
    (str(root / "web" / "index.html"), "runtime/web"),
    (str(root / "LEDERG-DirectLaunch.exe"), "."),
]
binaries = []
hiddenimports = []
for package in ("requests", "webview", "clr_loader", "pythonnet"):
    try:
        package_datas, package_binaries, package_hiddenimports = collect_all(package)
    except Exception:
        continue
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports
hiddenimports += [
    "webview.platforms.edgechromium",
    "webview.platforms.mshtml",
    "webview.platforms.winforms",
    "webview.guilib",
    "pythonnet",
    "clr_loader",
    "tkinter",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.font",
    "tkinter.ttk",
]

a = Analysis(
    ["bootstrap.py"],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "IPython", "jupyter"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="LEDERG CRMP",
    icon=(str(root / "LEDERG-CRMP-Avatar.ico") if (root / "LEDERG-CRMP-Avatar.ico").is_file() else None),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
