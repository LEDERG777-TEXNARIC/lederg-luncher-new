# LEDERG CR:MP Launcher

Native Windows launcher with a live HTML/CSS/JavaScript interface delivered from this repository. The EXE remains necessary: a normal website is not allowed to access local game files or launch a Windows process.

## How updates work

- At startup, bootstrap.py retrieves runtime-manifest.json from the main branch.
- The manifest is allow-listed and every runtime file is checked against its exact byte size and SHA-256 hash before use.
- The last verified version is cached in %LOCALAPPDATA%\LEDERG CRMP\runtime. When GitHub is unavailable, the launcher falls back to the verified cache or embedded first-run files.
- UI edits in web/index.html and changes to the runtime modules are picked up on the next launch without downloading a new EXE. The direct-connect helper is embedded in the Windows EXE and starts gta_sa.exe with the multiplayer DLL and configured server endpoint.
- Game files are updated independently through the existing client manifest, with per-file CRC validation and resumable downloads.

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