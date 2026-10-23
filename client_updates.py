from __future__ import annotations

import hashlib
import json
import os
import re
import time
import zlib
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import requests

STATE_FILENAME = ".lederg-client-state.json"
STATE_SCHEMA = 1
ALLOWED_SCHEMES = {"http", "https"}


def _safe_relative_path(value: str) -> PurePosixPath:
    raw = str(value or "").replace("\\", "/")
    parts = raw.split("/")
    relative = PurePosixPath(raw)
    if (
        not raw
        or not parts
        or any(part in ("", ".", "..") for part in parts)
        or relative.is_absolute()
        or raw.startswith("/")
        or ":" in parts[0]
        or "\x00" in raw
    ):
        raise ValueError(f"Unsafe client manifest path: {value!r}")
    return relative


def _validate_http_url(value: str, label: str, *, allow_local_http: bool = False) -> str:
    value = str(value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.netloc:
        raise ValueError(f"{label} must be an absolute HTTP or HTTPS URL.")
    if parsed.scheme.lower() == "http":
        host = (parsed.hostname or "").lower()
        allowed = host.endswith(".cloudpub.ru")
        if allow_local_http:
            allowed = allowed or host in {"localhost", "127.0.0.1", "::1"}
        if not allowed:
            raise ValueError(f"Unencrypted HTTP is not allowed for {label}.")
    if parsed.username or parsed.password:
        raise ValueError(f"Credentials are not allowed inside {label}.")
    return value


def validate_manifest(payload: dict) -> dict:
    """Validate untrusted remote metadata and return normalized paths and checksums."""
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ValueError("Unsupported client file manifest schema.")

    version = str(payload.get("version", "")).strip()
    install_root = str(payload.get("install_root", "GTA Criminal Russia")).strip()
    root_rel = PurePosixPath(install_root.replace("\\", "/"))
    if (
        not version
        or not install_root
        or root_rel.is_absolute()
        or len(root_rel.parts) != 1
        or root_rel.parts[0] in ("", ".", "..")
        or ":" in root_rel.parts[0]
        or "\x00" in install_root
    ):
        raise ValueError("Invalid install_root or version in client manifest.")

    base_url = _validate_http_url(
        payload.get("files_base_url", ""),
        "files_base_url",
        allow_local_http=True,
    ).rstrip("/") + "/"

    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("The client file manifest is empty.")

    files = []
    seen = set()
    total_bytes = 0
    for item in raw_files:
        if not isinstance(item, dict):
            raise ValueError("A client manifest entry must be an object.")
        relative = _safe_relative_path(item.get("path", ""))
        path = relative.as_posix()
        key = path.casefold()
        if key in seen:
            raise ValueError(f"Duplicate file path in client manifest: {path}")
        seen.add(key)
        try:
            size = int(item.get("size", -1))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid file size in client manifest: {path}") from exc
        crc32 = str(item.get("crc32", "")).strip().lower().removeprefix("0x")
        if size < 0 or not re.fullmatch(r"[0-9a-f]{8}", crc32):
            raise ValueError(f"Invalid size or CRC32 in client manifest: {path}")
        files.append({"path": path, "size": size, "crc32": crc32})
        total_bytes += size

    files.sort(key=lambda item: item["path"].casefold())
    canonical = {
        "install_root": root_rel.parts[0],
        "files": files,
    }
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()

    supplied_fingerprint = str(payload.get("fingerprint", "") or "").strip().lower()
    if supplied_fingerprint and not re.fullmatch(r"[0-9a-f]{64}", supplied_fingerprint):
        raise ValueError("Invalid fingerprint in client manifest.")

    result = dict(payload)
    result.update({
        "schema": 1,
        "version": version,
        "install_root": root_rel.parts[0],
        "files_base_url": base_url,
        "total_bytes": total_bytes,
        "file_count": len(files),
        "files": files,
        "fingerprint": digest,
    })
    return result


def fetch_files_manifest(url: str, http=None) -> dict:
    """Fetch and validate the current release manifest."""
    target = _validate_http_url(url, "manifest URL", allow_local_http=True)
    client_http = http or requests
    try:
        response = client_http.get(target, timeout=(10, 25))
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"Could not fetch LEDERG client manifest: {exc}") from exc
    return validate_manifest(payload)


def _load_state(install_root: Path) -> dict | None:
    state_path = install_root / STATE_FILENAME
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != STATE_SCHEMA:
        return None
    managed = payload.get("files")
    if not isinstance(managed, list):
        return None
    return payload


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp.write_text(data, encoding="utf-8")
    os.replace(temp, path)


def _crc32_file(path: Path) -> str:
    checksum = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum = zlib.crc32(chunk, checksum)
    return f"{checksum & 0xFFFFFFFF:08x}"


def _resolve_managed_path(root: Path, relative_value: str) -> Path:
    relative = _safe_relative_path(relative_value)
    target = root.joinpath(*relative.parts)
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Managed file path escapes the install root: {relative.as_posix()}") from exc
    return target


def inspect_installation(install_root: Path, manifest: dict) -> dict:
    """
    Compare installed files to a validated manifest.

    Existing state lets repeat checks compare size/mtime quickly and avoids
    hashing gigabytes repeatedly. Legacy installs are checksummed once.
    """
    manifest = validate_manifest(manifest)
    install_root = Path(install_root).resolve()
    if not install_root.is_dir():
        return {
            "update_available": True,
            "reason": "install_root_missing",
            "fingerprint": manifest["fingerprint"],
            "version": manifest["version"],
            "manifest": manifest,
            "missing": [item["path"] for item in manifest["files"]],
            "changed": [],
            "removed": [],
        }

    state = _load_state(install_root)
    prior_fingerprint = str((state or {}).get("fingerprint", ""))
    prior_items = (state or {}).get("files", [])
    prior = {}
    for item in prior_items:
        if not isinstance(item, dict):
            continue
        try:
            rel = _safe_relative_path(item.get("path", "")).as_posix()
            size = int(item.get("size", -1))
            crc = str(item.get("crc32", "")).lower()
            mtime_ns = int(item.get("mtime_ns", -1))
        except (ValueError, TypeError):
            continue
        if size >= 0 and re.fullmatch(r"[0-9a-f]{8}", crc):
            prior[rel.casefold()] = {
                "path": rel, "size": size, "crc32": crc, "mtime_ns": mtime_ns
            }

    expected_keys = {entry["path"].casefold() for entry in manifest["files"]}
    removed = []
    if state is not None:
        removed = [
            item["path"] for key, item in prior.items()
            if key not in expected_keys
        ]

    missing: list[str] = []
    changed: list[str] = []
    current_entries: list[dict] = []
    managed_paths = set()

    for entry in manifest["files"]:
        relative = entry["path"]
        expected_size = entry["size"]
        expected_crc = entry["crc32"]
        target = _resolve_managed_path(install_root, relative)
        managed_paths.add(relative.casefold())

        try:
            info = target.stat()
        except OSError:
            missing.append(relative)
            continue
        if not target.is_file() or info.st_size != expected_size:
            changed.append(relative)
            continue

        old = prior.get(relative.casefold())
        known_stat_matches = bool(
            old
            and old["size"] == info.st_size
            and old["mtime_ns"] == info.st_mtime_ns
        )

        if known_stat_matches:
            if old["size"] == expected_size and old["crc32"] == expected_crc:
                current_entries.append({
                    "path": relative, "size": expected_size,
                    "crc32": expected_crc, "mtime_ns": info.st_mtime_ns
                })
            else:
                # A previously verified file with unchanged size and mtime is
                # still the old release; there is no reason to hash it again.
                changed.append(relative)
            continue

        actual_crc = _crc32_file(target)
        if actual_crc != expected_crc:
            changed.append(relative)
            continue
        current_entries.append({
            "path": relative, "size": expected_size,
            "crc32": expected_crc, "mtime_ns": info.st_mtime_ns
        })

    update_available = bool(missing or changed or removed)
    report = {
        "update_available": update_available,
        "fingerprint": manifest["fingerprint"],
        "version": manifest["version"],
        "manifest": manifest,
        "missing": missing,
        "changed": changed,
        "removed": removed,
        "legacy_state": state is None,
        "prior_fingerprint": prior_fingerprint,
    }
    if not update_available:
        write_installed_state(install_root, manifest)
    return report


def remove_stale_managed_files(install_root: Path, manifest: dict) -> list[str]:
    """Remove only files previously managed by LEDERG and absent from the new manifest."""
    install_root = Path(install_root).resolve()
    manifest = validate_manifest(manifest)
    state = _load_state(install_root)
    if not state:
        return []
    keep = {entry["path"].casefold() for entry in manifest["files"]}
    deleted = []
    for item in state.get("files", []):
        if not isinstance(item, dict):
            continue
        try:
            relative = _safe_relative_path(item.get("path", "")).as_posix()
        except ValueError:
            continue
        if relative.casefold() in keep:
            continue
        target = _resolve_managed_path(install_root, relative)
        try:
            if target.is_file() and not target.is_symlink():
                target.unlink()
                deleted.append(relative)
                parent = target.parent
                while parent != install_root and parent.is_relative_to(install_root):
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
        except OSError:
            continue
    return deleted


def write_installed_state(install_root: Path, manifest: dict) -> Path:
    """Save a verified content fingerprint and fast mtime records for future checks."""
    install_root = Path(install_root).resolve()
    manifest = validate_manifest(manifest)
    files = []
    for entry in manifest["files"]:
        target = _resolve_managed_path(install_root, entry["path"])
        info = target.stat()
        if not target.is_file() or info.st_size != entry["size"]:
            raise RuntimeError(f"Installed file size does not match release: {entry['path']}")
        files.append({
            "path": entry["path"],
            "size": entry["size"],
            "crc32": entry["crc32"],
            "mtime_ns": info.st_mtime_ns,
        })
    state_path = install_root / STATE_FILENAME
    _atomic_json(state_path, {
        "schema": STATE_SCHEMA,
        "version": manifest["version"],
        "fingerprint": manifest["fingerprint"],
        "updated_at": int(time.time()),
        "files": files,
    })
    return state_path


def apply_successful_update(install_root: Path, manifest: dict) -> dict:
    """Prune old LEDERG-owned files and record the successfully installed manifest."""
    deleted = remove_stale_managed_files(install_root, manifest)
    state_path = write_installed_state(install_root, manifest)
    return {"deleted": deleted, "state_path": str(state_path)}


def manifest_update_summary(report: dict) -> str:
    counts = []
    if report.get("missing"):
        counts.append(f"{len(report['missing'])} missing")
    if report.get("changed"):
        counts.append(f"{len(report['changed'])} changed")
    if report.get("removed"):
        counts.append(f"{len(report['removed'])} obsolete")
    return ", ".join(counts) or "No file differences"
