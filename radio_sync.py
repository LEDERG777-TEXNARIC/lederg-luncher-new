from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse

import requests

RADIO_REPOSITORY = "LEDERG777-TEXNARIC/LEDERG-radio-crmp"
RADIO_BRANCH = "main"
TREE_URL = f"https://api.github.com/repos/{RADIO_REPOSITORY}/git/trees/{RADIO_BRANCH}?recursive=1"
RAW_BASE_URL = f"https://raw.githubusercontent.com/{RADIO_REPOSITORY}/{RADIO_BRANCH}/"
RADIO_RELATIVE_ROOT = Path("resound") / "radio"
RADIO_STATE_FILENAME = ".lederg-radio-state.json"
SUPPORTED_AUDIO_SUFFIXES = {".mp3", ".ogg", ".wav"}
STATE_SCHEMA = 1
USER_AGENT = "LEDERG-RadioSync/1.0"


def _safe_repo_track_path(value: str) -> PurePosixPath:
    raw = str(value or "").replace("\\", "/")
    parts = raw.split("/")
    relative = PurePosixPath(raw)
    if (
        not raw
        or len(parts) != 2
        or any(part in ("", ".", "..") or part.startswith(".") for part in parts)
        or relative.is_absolute()
        or ":" in parts[0]
        or "\x00" in raw
    ):
        raise ValueError(f"Unsafe radio track path: {value!r}")
    for part in parts:
        stem = part.split(".", 1)[0].casefold()
        if stem in {"con", "prn", "aux", "nul"} or re.fullmatch(r"(com|lpt)[1-9]", stem):
            raise ValueError(f"Windows-reserved radio track path: {value!r}")
    if Path(parts[1]).suffix.casefold() not in SUPPORTED_AUDIO_SUFFIXES:
        raise ValueError(f"Unsupported audio format in radio repository: {value!r}")
    return relative


def _git_blob_sha_bytes(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _git_blob_sha_file(path: Path) -> str:
    size = path.stat().st_size
    h = hashlib.sha1(f"blob {size}\0".encode("ascii"))
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("schema") == STATE_SCHEMA and isinstance(value.get("files"), list):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return {"schema": STATE_SCHEMA, "files": []}


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _public_url(url: str, allow_local_http: bool = False) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() == "https" and parsed.netloc:
        return url
    if allow_local_http and parsed.scheme.lower() == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"} and parsed.port:
        return url
    raise ValueError("Radio sync only accepts HTTPS GitHub URLs (HTTP is allowed for local tests).")


def _get_tree(client_http, tree_url: str) -> list[dict]:
    response = client_http.get(tree_url, timeout=(10, 30))
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
        raise RuntimeError("GitHub returned an invalid radio repository tree.")
    if payload.get("truncated"):
        raise RuntimeError("Radio repository tree is truncated; refusing to delete stations using a partial listing.")
    result = []
    seen_tracks = set()
    station_case_map = {}
    for item in payload["tree"]:
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        path = str(item.get("path", ""))
        parts = path.replace("\\", "/").split("/")
        if len(parts) != 2:
            continue
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"Unsafe station or track path in GitHub tree: {path!r}")
        if parts[0].startswith("."):
            continue
        if Path(parts[1]).suffix.casefold() not in SUPPORTED_AUDIO_SUFFIXES:
            continue
        relative = _safe_repo_track_path(path)
        station = relative.parts[0]
        station_key = station.casefold()
        if station_key in station_case_map and station_case_map[station_key] != station:
            raise ValueError(f"Station folder names collide on Windows: {station_case_map[station_key]!r} and {station!r}")
        station_case_map[station_key] = station
        key = relative.as_posix().casefold()
        if key in seen_tracks:
            raise ValueError(f"Duplicate case-insensitive track path in radio repository: {path}")
        seen_tracks.add(key)
        try:
            size = int(item.get("size", -1))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid size in radio repository tree: {path}") from exc
        sha = str(item.get("sha", "")).lower()
        if size <= 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError(f"Invalid Git blob metadata in radio repository: {path}")
        result.append({"path": relative.as_posix(), "station": station, "size": size, "sha": sha})
    result.sort(key=lambda item: item["path"].casefold())
    return result


def _download_track(
    client_http,
    url: str,
    target: Path,
    expected_size: int,
    expected_sha: str,
    timeout=(15, 60),
) -> bool:
    """Download one track with resumable partial files and verify the Git blob SHA."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.resolve().relative_to(target.parent.parent.resolve())
    except ValueError as exc:
        raise ValueError(f"Track path escapes the radio directory: {target}") from exc
    part = target.with_name(target.name + ".lederg.part")

    if target.is_file() and target.stat().st_size == expected_size:
        if _git_blob_sha_file(target) == expected_sha:
            return False
    if part.is_file() and part.stat().st_size > expected_size:
        part.unlink()

    last_error = None
    for attempt in range(4):
        offset = part.stat().st_size if part.is_file() else 0
        if offset > expected_size:
            part.unlink(missing_ok=True)
            offset = 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            with client_http.get(url, headers=headers, stream=True, timeout=timeout) as response:
                if offset and response.status_code == 206:
                    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""), re.I)
                    if not match or int(match.group(1)) != offset or int(match.group(3)) != expected_size:
                        raise RuntimeError("GitHub returned an invalid Range response for a radio track.")
                    mode = "ab"
                else:
                    response.raise_for_status()
                    if response.status_code == 206:
                        match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""), re.I)
                        if not match or int(match.group(1)) != 0 or int(match.group(3)) != expected_size:
                            raise RuntimeError("GitHub returned an invalid partial response for a radio track.")
                    offset = 0
                    mode = "wb"

                received = offset
                with part.open(mode) as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        received += len(chunk)
                        stream.write(chunk)
            if received != expected_size or part.stat().st_size != expected_size:
                raise ConnectionError(f"Partial radio track received ({part.stat().st_size}/{expected_size} bytes).")
            if _git_blob_sha_file(part) != expected_sha:
                part.unlink(missing_ok=True)
                raise RuntimeError("GitHub blob SHA mismatch for a radio track.")
            os.replace(part, target)
            return True
        except Exception as exc:
            last_error = exc
            if part.is_file() and part.stat().st_size > expected_size:
                part.unlink(missing_ok=True)
            if attempt < 3:
                continue
    raise RuntimeError(f"Could not download radio track {target.name}: {last_error}") from last_error


def sync_radio_repository(
    game_root: Path,
    http=None,
    tree_url: str | None = None,
    raw_base_url: str | None = None,
) -> dict:
    """
    Sync station folders from the public GitHub repository.

    A top-level folder becomes a station if it contains at least one MP3/OGG/WAV.
    Only files recorded as LEDERG-managed are ever removed on a later sync.
    """
    client_http = http or requests.Session()
    if hasattr(client_http, "headers"):
        client_http.headers.update({"User-Agent": USER_AGENT})
    tree_url = _public_url(tree_url or TREE_URL, allow_local_http=True)
    raw_base_url = _public_url(raw_base_url or RAW_BASE_URL, allow_local_http=True).rstrip("/") + "/"

    tree = _get_tree(client_http, tree_url)
    radio_root = (Path(game_root) / RADIO_RELATIVE_ROOT).resolve()
    game_root_resolved = Path(game_root).resolve()
    try:
        radio_root.relative_to(game_root_resolved)
    except ValueError as exc:
        raise ValueError("Radio directory resolves outside the client root.") from exc
    state_path = radio_root.parent / RADIO_STATE_FILENAME
    old_state = _load_state(state_path)
    old_files = {}
    for item in old_state.get("files", []):
        if isinstance(item, dict):
            try:
                rel = _safe_repo_track_path(item.get("path", "")).as_posix()
            except ValueError:
                continue
            old_files[rel.casefold()] = item

    expected = {item["path"].casefold(): item for item in tree}
    downloaded = 0
    skipped = 0
    failures = []
    local_records = []

    for item in tree:
        relative = _safe_repo_track_path(item["path"])
        target = radio_root.joinpath(*relative.parts)
        try:
            target.resolve().relative_to(radio_root)
        except ValueError as exc:
            raise ValueError(f"Radio track resolves outside radio root: {item['path']}") from exc

        old = old_files.get(item["path"].casefold())
        try:
            if (
                old
                and str(old.get("sha", "")).lower() == item["sha"]
                and int(old.get("size", -1)) == item["size"]
                and target.is_file()
                and target.stat().st_size == item["size"]
                and int(old.get("mtime_ns", -1)) == target.stat().st_mtime_ns
            ):
                changed = False
            else:
                encoded = "/".join(quote(segment, safe="") for segment in relative.parts)
                changed = _download_track(
                    client_http,
                    raw_base_url + encoded,
                    target,
                    item["size"],
                    item["sha"],
                )
            if changed:
                downloaded += 1
            else:
                skipped += 1
            info = target.stat()
            local_records.append({
                "path": item["path"],
                "station": item["station"],
                "sha": item["sha"],
                "size": item["size"],
                "mtime_ns": info.st_mtime_ns,
            })
        except Exception as exc:
            failures.append(f"{item['path']}: {exc}")

    # Do not prune any previously managed content if even one current track failed.
    if failures:
        return {
            "ok": False, "stations": sorted({item["station"] for item in tree}, key=str.casefold),
            "downloaded": downloaded, "skipped": skipped, "removed": [],
            "tracks": len(tree), "errors": failures,
        }

    current = {item["path"].casefold() for item in tree}
    removed = []
    for key, old in old_files.items():
        path_text = str(old.get("path", ""))
        if key in current:
            continue
        try:
            relative = _safe_repo_track_path(path_text)
            target = radio_root.joinpath(*relative.parts)
            target.resolve().relative_to(radio_root)
            if target.is_file() and not target.is_symlink():
                target.unlink()
                removed.append(path_text)
            part = target.with_name(target.name + ".lederg.part")
            if part.is_file() and not part.is_symlink():
                part.unlink()
        except (OSError, ValueError):
            continue

    for directory, subdirs, files in os.walk(radio_root, topdown=False):
        folder = Path(directory)
        for name in files:
            path = folder / name
            if path.name.endswith(".lederg.part") and path.is_file():
                # Interrupted partials for still-current tracks remain resumable.
                rel = path.relative_to(radio_root).as_posix()
                if rel.removesuffix(".lederg.part").casefold() not in current:
                    path.unlink(missing_ok=True)
        if folder != radio_root:
            try:
                folder.rmdir()
            except OSError:
                pass

    _atomic_write(state_path, {
        "schema": STATE_SCHEMA,
        "repository": RADIO_REPOSITORY,
        "branch": RADIO_BRANCH,
        "files": local_records,
    })
    stations = sorted({item["station"] for item in tree}, key=str.casefold)
    return {
        "ok": True,
        "stations": stations,
        "downloaded": downloaded,
        "skipped": skipped,
        "removed": removed,
        "tracks": len(tree),
        "errors": [],
    }
