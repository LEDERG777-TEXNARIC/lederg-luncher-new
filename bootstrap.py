from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

REPOSITORY = "LEDERG777-TEXNARIC/lederg-luncher-crmp"
BRANCH = "main"
MANIFEST_URL = f"https://raw.githubusercontent.com/{REPOSITORY}/{BRANCH}/runtime-manifest.json"
USER_HOME = Path(
    os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
) / "LEDERG CRMP"
RUNTIME_HOME = USER_HOME / "runtime"
MANIFEST_CACHE = USER_HOME / "runtime-manifest.json"
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
BUNDLED_RUNTIME = BUNDLE_ROOT / "runtime"
EXPECTED_FILES = {
    "client_updates.py",
    "radio_sync.py",
    "launcher.py",
    "web_runtime.py",
    "web/index.html",
}
USER_AGENT = "LEDERG-CRMP-Launcher/2.0"


def _log(message: str) -> None:
    try:
        USER_HOME.mkdir(parents=True, exist_ok=True)
        with (USER_HOME / "bootstrap.log").open("a", encoding="utf-8") as stream:
            stream.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + message + "\n")
    except OSError:
        pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_manifest(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ValueError("Unsupported runtime manifest schema.")
    version = str(payload.get("version", "")).strip()
    files = payload.get("files")
    if not version or not isinstance(files, list) or not files:
        raise ValueError("Runtime manifest requires a version and file list.")
    normalized = {}
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Runtime file entry must be an object.")
        raw_path = str(item.get("path", "")).replace("\\", "/")
        relative = PurePosixPath(raw_path)
        if (
            not raw_path
            or raw_path not in EXPECTED_FILES
            or relative.is_absolute()
            or any(part in ("", ".", "..") for part in relative.parts)
            or raw_path in normalized
        ):
            raise ValueError(f"Unexpected runtime file path: {raw_path!r}")
        size = int(item.get("size", -1))
        digest = str(item.get("sha256", "")).lower().strip()
        if size < 1 or size > 20 * 1024 * 1024 or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"Invalid size or SHA-256 for {raw_path}.")
        normalized[raw_path] = {"path": raw_path, "size": size, "sha256": digest}
    if set(normalized) != EXPECTED_FILES:
        missing = sorted(EXPECTED_FILES - set(normalized))
        raise ValueError("Runtime manifest file list is incomplete: " + ", ".join(missing))
    return {"schema": 1, "version": version, "files": [normalized[p] for p in sorted(normalized)]}


def _read_json(path: Path) -> dict | None:
    try:
        return _safe_manifest(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _embedded_manifest() -> dict | None:
    for candidate in (BUNDLE_ROOT / "runtime-manifest.json", BUNDLED_RUNTIME / "runtime-manifest.json"):
        manifest = _read_json(candidate)
        if manifest:
            return manifest
    return None


def _fetch_manifest() -> dict:
    request = urllib.request.Request(MANIFEST_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=7) as response:
        if response.status != 200:
            raise OSError(f"GitHub returned HTTP {response.status} for runtime manifest.")
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Runtime manifest is unexpectedly large.")
    return _safe_manifest(json.loads(raw.decode("utf-8-sig")))


def _download(url: str, destination: Path, expected_size: int, expected_sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".download")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response, temporary.open("wb") as out:
            if response.status != 200:
                raise OSError(f"GitHub returned HTTP {response.status} for {url}.")
            total = 0
            digest = hashlib.sha256()
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size:
                    raise ValueError(f"File is larger than its manifest size: {destination.name}")
                digest.update(chunk)
                out.write(chunk)
        if total != expected_size:
            raise ValueError(f"Downloaded size mismatch for {destination.name}: {total}/{expected_size}")
        if digest.hexdigest() != expected_sha256:
            raise ValueError(f"SHA-256 mismatch for {destination.name}")
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _valid_file(path: Path, entry: dict) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != entry["size"]:
            return False
        return _sha256(path) == entry["sha256"]
    except OSError:
        return False


def _candidate_sources(relative_path: str):
    yield RUNTIME_HOME / relative_path
    yield BUNDLED_RUNTIME / relative_path


def _ensure_runtime(manifest: dict) -> Path:
    RUNTIME_HOME.mkdir(parents=True, exist_ok=True)
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    release_hash = hashlib.sha256(manifest_bytes).hexdigest()[:12]
    version_label = "".join(
        character for character in manifest["version"]
        if character.isalnum() or character in "-_."
    )[:40] or "release"
    release_home = RUNTIME_HOME / "releases" / f"{version_label}-{release_hash}"
    release_home.mkdir(parents=True, exist_ok=True)
    for entry in manifest["files"]:
        relative = entry["path"]
        target = release_home / relative
        if _valid_file(target, entry):
            continue
        bundled = BUNDLED_RUNTIME / relative
        if _valid_file(bundled, entry):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundled, target)
            continue
        raw_url = (
            f"https://raw.githubusercontent.com/{REPOSITORY}/{BRANCH}/"
            + urllib.parse.quote(relative, safe="/")
        )
        _log(f"Downloading runtime file {relative} for version {manifest['version']}.")
        _download(raw_url, target, entry["size"], entry["sha256"])
    for entry in manifest["files"]:
        if not _valid_file(release_home / entry["path"], entry):
            raise RuntimeError(f"Runtime verification failed: {entry['path']}")
    return release_home


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load runtime module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    try:
        USER_HOME.mkdir(parents=True, exist_ok=True)
        bundled_manifest = _embedded_manifest()
        cached_manifest = _read_json(MANIFEST_CACHE)
        try:
            manifest = _fetch_manifest()
            try:
                runtime_home = _ensure_runtime(manifest)
            except Exception as update_error:
                fallback = cached_manifest if cached_manifest and cached_manifest != manifest else None
                if fallback is None and bundled_manifest and bundled_manifest != manifest:
                    fallback = bundled_manifest
                if fallback is None:
                    raise
                _log(
                    f"Latest runtime unavailable ({update_error!r}); "
                    f"using verified fallback {fallback['version']}."
                )
                manifest = fallback
                runtime_home = _ensure_runtime(manifest)
            MANIFEST_CACHE.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            _log(f"Runtime manifest ready: {manifest['version']}.")
        except Exception as network_error:
            _log(f"GitHub manifest unavailable: {network_error!r}")
            manifest = cached_manifest or bundled_manifest
            if not manifest:
                raise RuntimeError(
                    "Нет соединения с GitHub, а кэш лаунчера ещё не создан. "
                    "Запусти установщик один раз с доступом к интернету."
                ) from network_error
            runtime_home = _ensure_runtime(manifest)
            _log(f"Using cached/embedded runtime manifest {manifest['version']}.")
        os.environ["LEDERG_RUNTIME_DIR"] = str(runtime_home)
        os.environ["LEDERG_WEB_ROOT"] = str(runtime_home / "web")
        _load_module("client_updates", runtime_home / "client_updates.py")
        _load_module("radio_sync", runtime_home / "radio_sync.py")
        _load_module("launcher", runtime_home / "launcher.py")
        web_runtime = _load_module("web_runtime", runtime_home / "web_runtime.py")
        _log(f"Starting native shell for runtime version {manifest['version']}.")
        web_runtime.start()
        return 0
    except Exception as exc:
        _log("FATAL: " + traceback.format_exc())
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "Не удалось запустить LEDERG CR:MP.\n\n" + str(exc) +
                "\n\nПодробности: %LOCALAPPDATA%\\LEDERG CRMP\\bootstrap.log",
                "LEDERG CR:MP",
                0x10,
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
