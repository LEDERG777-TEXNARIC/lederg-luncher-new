# LEDERG CR:MP Launcher

Native Windows launcher with a live HTML/CSS/JavaScript interface delivered from this repository. The EXE remains necessary: a normal website is not allowed to access local game files or launch a Windows process.

## Runtime updates and self-healing client checks

- At startup, `bootstrap.py` retrieves `runtime-manifest.json` from `main`. Runtime files are allow-listed and verified against byte size and SHA-256 before loading; the last verified copy is cached under `%LOCALAPPDATA%\LEDERG CRMP\runtime`.
- UI edits and runtime-module changes can be delivered through the verified manifest without rebuilding the EXE. The Play path starts the bundled `LEDERG-DirectLaunch.exe` helper invisibly, launches `gta_sa.exe`, loads `multiplayer_c/san_andreas_multiplayer.dll`, and passes the configured host, port, and nickname. It does not open `multiplayer_browser_cr.exe`; the helper's console is suppressed on Windows.
- The client checker validates the CR:MP folder layout, required binaries and x86 PE headers. The per-file manifest records the installed release fingerprint and per-file CRC32/size, so an old or modified client is detected even when its folder name is unchanged. On later starts it checks critical binaries and modified files; a complete integrity pass is performed weekly.
- Client installation and repair prefer the live CloudPub per-file manifest (`client-manifest.json`). The launcher compares each local file against the release size/CRC32, skips matching files, resumes interrupted downloads, and downloads only missing or changed files. The Google Drive ZIP remains a fallback for older release manifests. After `0xC0000005`, it attempts to collect Windows Application Error / WER records and then performs at most one automatic full-client repair per installation every 24 hours. This prevents endless download/crash loops.
- The supplied CR:MP folder contains an ENB `d3d9.dll` graphics proxy. The launcher disables it only when Windows identifies that exact local file as the faulting module and the ENB configuration/license files are present. The original DLL is preserved as `d3d9.dll.lederg-disabled.bak`; system DLLs are never changed.
- Legacy installations without an integrity ledger are checked for required structure and PE headers first. Their trusted file baseline is created after a successful archive-based repair/reinstall.

## Development

Use Python 3.14 x64 on Windows.

1. Install dependencies with: python -m pip install -r requirements.txt
2. Refresh the runtime manifest with: python tools/update_runtime_manifest.py
3. Run tests with: python -m unittest discover -v
4. On Windows, compile the 32-bit direct-connect helper:
   `%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe /nologo /platform:x86 /target:exe /out:LEDERG-DirectLaunch.exe direct_launcher.cs`
5. Build with: pyinstaller --clean --noconfirm "LEDERG CRMP.spec"

Run tools/update_runtime_manifest.py after changing launcher.py, client_updates.py, radio_sync.py, web_runtime.py, or web/index.html; commit the updated manifest in the same change.

## Release model

GitHub stores the small native bootstrap source and the live UI. A release EXE embeds a known-good fallback copy for first launch/offline startup; subsequent Python module and UI updates are downloaded and verified using the runtime manifest.

## LEDERG PANEL hotkey

With the updated Windows launcher open, press physical **X** while the CR:MP game window is foreground to send `/xpanel`. The server checks the account rank again; rank 13 and server-wide actions are restricted to the reserved LEDERG founder account. `/xpanel` remains available as a manual chat-command fallback.

The launcher fetches verified runtime updates from this repository at startup, so this hotkey update does not require a new EXE build.
