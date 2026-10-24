from __future__ import annotations

import ctypes
import hashlib
import ipaddress
import itertools
import json
import os
import queue
import re
import shutil
import socket
import stat
import sys
import subprocess
import threading
import time
import tkinter as tk
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox
from html import unescape
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse, urlunparse

import requests

from client_updates import (
    STATE_FILENAME,
    apply_successful_update,
    fetch_files_manifest,
    inspect_installation,
    validate_manifest,
)
from radio_sync import sync_radio_repository

APP_NAME = "LEDERG CR:MP"
# Public config is intentionally separated from the launcher binary so server
# addresses and client releases can be changed without rebuilding the EXE.
SERVER_IP_URL = "https://raw.githubusercontent.com/LEDERG777-TEXNARIC/lederg-ip-crmp/main/ip"
CLIENT_MANIFEST_URL = "https://raw.githubusercontent.com/LEDERG777-TEXNARIC/lederg-client/main/client.json"
DRIVE_FILE_ID = "1981J9qVnGbEjLK7lXg7L2AO1br0ATRct"
DRIVE_DOWNLOAD_URL = (
    "https://drive.usercontent.google.com/download"
    f"?id={DRIVE_FILE_ID}&export=download"
)
# The full game archive is deliberately pinned to this single Google Drive file.
# Per-file/Cloudpub manifests are disabled so no game payload comes from another host.
ZIP_URL = DRIVE_DOWNLOAD_URL
CLIENT_FILES_MANIFEST_URL = "https://oddly-hopeful-rudd.cloudpub.ru/client-manifest.json"
ZIP_SIZE = 0
SERVER_HOST = "188.127.241.8"
SERVER_PORT = 1130
DEFAULT_LAUNCH_MODE = "direct"
DEFAULT_CLIENT_EXECUTABLE = "multiplayer_c/multiplayer_browser_cr.exe"
DIRECT_LAUNCHER_NAME = "LEDERG-DirectLaunch.exe"

CLIENT_INTEGRITY_FILENAME = ".lederg-client-integrity.json"
CLIENT_REQUIRED_DIRECTORIES = (
    "anim",
    "audio",
    "audio/CONFIG",
    "audio/SFX",
    "audio/streams",
    "cleo",
    "data",
    "models",
    "movies",
    "text",
    "multiplayer_c",
)
CLIENT_REQUIRED_FILES = (
    "gta_sa.exe",
    "cleo.asi",
    "eax.dll",
    "GTAINTERFACE.dll",
    "LibMP.dll",
    "ogg.dll",
    "anim/anim.img",
    "multiplayer_c/multiplayer_browser_cr.exe",
    "multiplayer_c/grand_theft_auto_san_andreas.dll",
    "multiplayer_c/san_andreas_multiplayer.dll",
    # Runtime dependencies visible in the supplied GTA Criminal Russia Drive folder.
    "multiplayer_c/adapt.dll",
    "multiplayer_c/adaptation_helper.dll",
    "multiplayer_c/bass.dll",
    "multiplayer_c/eax.dll",
    "multiplayer_c/ogg.dll",
    "multiplayer_c/vorbis.dll",
    "multiplayer_c/vorbisFile.dll",
    "vorbis.dll",
    "vorbisFile.dll",
    "vorbisHooked.dll",
    "data/handling.cfg",
    "models/gta3.img",
    "models/gamemod.img",
    "text/american.gxt",
)
CLIENT_PE_FILES = (
    "gta_sa.exe",
    "cleo.asi",
    "eax.dll",
    "GTAINTERFACE.dll",
    "LibMP.dll",
    "ogg.dll",
    "multiplayer_c/multiplayer_browser_cr.exe",
    "multiplayer_c/grand_theft_auto_san_andreas.dll",
    "multiplayer_c/san_andreas_multiplayer.dll",
    "multiplayer_c/adapt.dll",
    "multiplayer_c/adaptation_helper.dll",
    "multiplayer_c/bass.dll",
    "multiplayer_c/eax.dll",
    "multiplayer_c/ogg.dll",
    "multiplayer_c/vorbis.dll",
    "multiplayer_c/vorbisFile.dll",
    "vorbis.dll",
    "vorbisFile.dll",
    "vorbisHooked.dll",
)
CLIENT_FULL_SCAN_INTERVAL_SECONDS = 7 * 24 * 60 * 60
ENB_DISABLED_BACKUP = "d3d9.dll.lederg-disabled.bak"

BG = "#000000"
BG_SOFT = "#050505"
RED = "#ff263f"
RED_HOT = "#ff4358"
RED_DARK = "#21060b"
WHITE = "#f4f4f4"
MUTED = "#858585"
MUTED2 = "#505050"
TRACK = "#171717"
GREEN = "#ff263f"

HTTP = requests.Session()
HTTP.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
    )
})


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _x86_pe_error(path: Path) -> str | None:
    """Check DOS/PE signatures and the expected 32-bit x86 machine type."""
    try:
        with path.open("rb") as stream:
            dos = stream.read(64)
            if len(dos) < 64 or dos[:2] != b"MZ":
                return "отсутствует заголовок MZ"
            pe_offset = int.from_bytes(dos[0x3C:0x40], "little")
            if pe_offset < 64 or pe_offset > path.stat().st_size - 6:
                return "неверный указатель на PE-заголовок"
            stream.seek(pe_offset)
            pe = stream.read(6)
        if len(pe) != 6 or pe[:4] != b"PE\0\0":
            return "неверная сигнатура PE"
        machine = int.from_bytes(pe[4:6], "little")
        if machine != 0x014C:
            return f"ожидается x86 (0x014C), обнаружен machine=0x{machine:04X}"
        return None
    except OSError as exc:
        return f"не удалось прочитать PE-заголовок: {exc}"


def write_client_integrity_manifest(
    install_root: Path,
    entries: list[dict],
    *,
    source: str = "google-drive-archive",
    last_full_scan_at: float | None = None,
) -> Path:
    """Persist hashes for files verified while extracting the downloaded client archive."""
    root = Path(install_root).resolve()
    normalized = []
    seen: set[str] = set()
    for item in entries:
        raw = str(item.get("path") or "").replace("\\", "/")
        relative = PurePosixPath(raw)
        if (
            not raw
            or relative.is_absolute()
            or raw.startswith("/")
            or any(part in ("", ".", "..") for part in relative.parts)
            or ":" in relative.parts[0]
            or "\x00" in raw
        ):
            raise ValueError(f"Unsafe client integrity path: {raw!r}")
        key = relative.as_posix().casefold()
        if key in seen:
            raise ValueError(f"Duplicate path in client integrity manifest: {raw}")
        seen.add(key)
        expected_size = int(item.get("size", -1))
        digest = str(item.get("sha256") or "").lower()
        if expected_size < 0 or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"Invalid integrity size or SHA-256 for {raw}")
        target = root.joinpath(*relative.parts)
        try:
            target.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Integrity path escapes client root: {raw}") from exc
        if target.is_symlink() or not target.is_file():
            raise RuntimeError(f"Integrity file is missing or unsafe: {raw}")
        info = target.stat()
        if info.st_size != expected_size:
            raise RuntimeError(f"Integrity size mismatch immediately after install: {raw}")
        normalized.append({
            "path": relative.as_posix(),
            "size": expected_size,
            "sha256": digest,
            "crc32": str(item.get("crc32") or "").lower(),
            "mtime_ns": info.st_mtime_ns,
        })
    if not normalized:
        raise RuntimeError("Archive extraction did not produce an integrity inventory.")
    normalized.sort(key=lambda item: item["path"].casefold())
    payload = {
        "schema": 1,
        "source": str(source),
        "created_at": int(time.time()),
        "last_full_scan_at": int(last_full_scan_at if last_full_scan_at is not None else time.time()),
        "files": normalized,
    }
    destination = root / CLIENT_INTEGRITY_FILENAME
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def finalize_client_integrity_manifest(
    install_root: Path, extraction_root: Path, inventory_path: Path
) -> Path:
    """Rebase ZIP inventory paths from extraction destination to the discovered game root."""
    root = Path(install_root).resolve()
    base = Path(extraction_root).resolve()
    inventory = json.loads(Path(inventory_path).read_text(encoding="utf-8"))
    if not isinstance(inventory, dict) or inventory.get("schema") != 1:
        raise ValueError("Invalid temporary client integrity inventory.")
    raw_entries = inventory.get("files")
    if not isinstance(raw_entries, list):
        raise ValueError("Client integrity inventory has no file list.")
    entries = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("path") or "").replace("\\", "/")
        relative = PurePosixPath(raw)
        if not raw or relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
            raise ValueError(f"Unsafe path in extracted inventory: {raw!r}")
        extracted = base.joinpath(*relative.parts)
        try:
            extracted_resolved = extracted.resolve()
            rebased = extracted_resolved.relative_to(root)
        except (OSError, ValueError):
            # ZIPs may contain companion files beside the game root; they aren't
            # managed by the client-root integrity manifest.
            continue
        if extracted.is_symlink() or not extracted.is_file():
            continue
        entries.append({
            "path": rebased.as_posix(),
            "size": int(item.get("size", -1)),
            "sha256": str(item.get("sha256") or ""),
            "crc32": str(item.get("crc32") or ""),
        })
    if not entries:
        raise RuntimeError("No extracted archive files were found inside the game root.")
    return write_client_integrity_manifest(root, entries, source="google-drive-archive")


def inspect_client_installation(
    install_root: Path,
    *,
    full_scan: bool = False,
    progress_callback=None,
) -> dict:
    """Validate the CR:MP folder layout, PE architecture, and installed-file hashes."""
    root = Path(install_root).resolve()
    issues: list[str] = []
    warnings: list[str] = []
    missing_files: list[str] = []
    checked_files = 0
    integrity_mode = "structural-only"
    inventory_path = root / CLIENT_INTEGRITY_FILENAME

    if not root.is_dir():
        issues.append(f"Не найдена папка клиента: {root}")
    else:
        for relative in CLIENT_REQUIRED_DIRECTORIES:
            if not (root / relative).is_dir():
                issues.append(f"Отсутствует папка клиента: {relative}")
        for relative in CLIENT_REQUIRED_FILES:
            target = root / relative
            if not target.is_file():
                missing_files.append(relative)
                issues.append(f"Отсутствует обязательный файл: {relative}")
                continue
            try:
                if target.stat().st_size <= 0:
                    missing_files.append(relative)
                    issues.append(f"Обязательный файл пустой: {relative}")
            except OSError as exc:
                missing_files.append(relative)
                issues.append(f"Не удалось проверить файл {relative}: {exc}")
        for relative in CLIENT_PE_FILES:
            target = root / relative
            if target.is_file():
                pe_error = _x86_pe_error(target)
                if pe_error:
                    issues.append(f"Повреждён или не подходит бинарный файл {relative}: {pe_error}")

        if (root / "d3d9.dll").is_file() and (root / "enbseries.ini").is_file():
            warnings.append(
                "Найдена локальная графическая обёртка ENB (d3d9.dll); "
                "она будет отключена только если Windows подтвердит её связь со сбоем."
            )

        if inventory_path.is_file():
            try:
                payload = json.loads(inventory_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or payload.get("schema") != 1:
                    raise ValueError("unsupported schema")
                entries = payload.get("files")
                if not isinstance(entries, list) or not entries:
                    raise ValueError("empty file inventory")
                if len(entries) > 250000:
                    raise ValueError("file inventory is too large")
                last_scan = float(payload.get("last_full_scan_at") or 0)
                if not full_scan and time.time() - last_scan >= CLIENT_FULL_SCAN_INTERVAL_SECONDS:
                    full_scan = True
                total_bytes = sum(max(0, int(entry.get("size", 0))) for entry in entries if isinstance(entry, dict))
                processed_bytes = 0
                seen: set[str] = set()
                changed_manifest = False
                critical_paths = {name.casefold() for name in CLIENT_PE_FILES}
                integrity_mode = "full" if full_scan else "fast"
                for entry in entries:
                    if not isinstance(entry, dict):
                        issues.append("Некорректная запись контрольной суммы клиента")
                        continue
                    raw = str(entry.get("path") or "").replace("\\", "/")
                    relative = PurePosixPath(raw)
                    if (
                        not raw
                        or relative.is_absolute()
                        or raw.startswith("/")
                        or any(part in ("", ".", "..") for part in relative.parts)
                        or ":" in relative.parts[0]
                        or "\x00" in raw
                    ):
                        issues.append(f"Небезопасный путь в манифесте клиента: {raw!r}")
                        continue
                    key = relative.as_posix().casefold()
                    if key in seen:
                        issues.append(f"Повтор пути в манифесте клиента: {raw}")
                        continue
                    seen.add(key)
                    expected_size = int(entry.get("size", -1))
                    expected_hash = str(entry.get("sha256") or "").lower()
                    if expected_size < 0 or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                        issues.append(f"Некорректная контрольная сумма: {raw}")
                        continue
                    if (
                        key == "d3d9.dll"
                        and (root / ENB_DISABLED_BACKUP).is_file()
                    ):
                        processed_bytes += expected_size
                        continue
                    target = root.joinpath(*relative.parts)
                    try:
                        target.resolve().relative_to(root)
                    except ValueError:
                        issues.append(f"Путь файла вышел за пределы клиента: {raw}")
                        continue
                    try:
                        info = target.stat()
                    except OSError:
                        issues.append(f"Пропал установленный файл: {raw}")
                        processed_bytes += expected_size
                        if progress_callback:
                            progress_callback(processed_bytes, total_bytes, raw)
                        continue
                    if not target.is_file() or target.is_symlink():
                        issues.append(f"Ожидался обычный файл: {raw}")
                        processed_bytes += expected_size
                        if progress_callback:
                            progress_callback(processed_bytes, total_bytes, raw)
                        continue
                    if info.st_size != expected_size:
                        issues.append(
                            f"Размер установленного файла изменился: {raw} "
                            f"({info.st_size} вместо {expected_size} байт)"
                        )
                        processed_bytes += expected_size
                        if progress_callback:
                            progress_callback(processed_bytes, total_bytes, raw)
                        continue
                    old_mtime = int(entry.get("mtime_ns", -1))
                    must_hash = full_scan or info.st_mtime_ns != old_mtime or key in critical_paths
                    if must_hash:
                        actual_hash = _sha256_file(target)
                        checked_files += 1
                        if actual_hash != expected_hash:
                            issues.append(f"Не совпала SHA-256 контрольная сумма: {raw}")
                        elif info.st_mtime_ns != old_mtime:
                            entry["mtime_ns"] = info.st_mtime_ns
                            changed_manifest = True
                    processed_bytes += expected_size
                    if progress_callback:
                        progress_callback(processed_bytes, total_bytes, raw)
                if full_scan and not issues:
                    payload["last_full_scan_at"] = int(time.time())
                    changed_manifest = True
                if changed_manifest and not issues:
                    temporary = inventory_path.with_name(inventory_path.name + ".tmp")
                    temporary.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    os.replace(temporary, inventory_path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                issues.append(f"Не удалось проверить контрольные суммы клиента: {exc}")
        else:
            warnings.append(
                "Для этой старой установки нет эталонного манифеста; "
                "проверены обязательные файлы и их PE-заголовки."
            )

    healthy = not issues
    if healthy:
        summary = (
            f"Проверены обязательные компоненты и структура клиента; "
            f"проверок SHA-256: {checked_files} ({integrity_mode})."
        )
    else:
        summary = f"Найдены проблемы клиента: {len(issues)}."
    return {
        "healthy": healthy,
        "root": str(root),
        "issues": issues,
        "warnings": warnings,
        "missing_files": missing_files,
        "checked_files": checked_files,
        "integrity_mode": integrity_mode,
        "summary": summary,
    }



def select_client_crash_events(
    events: str | list[str] | tuple[str, ...], install_root: Path
) -> list[str]:
    """Keep recent access-violation events whose Windows paths point inside this install."""
    texts = [events] if isinstance(events, str) else list(events or [])
    root = os.path.normcase(os.path.abspath(os.fspath(install_root))).replace("/", "\\").rstrip("\\")
    if not root:
        return []
    root_folded = root.casefold()
    selected = []
    for event in texts:
        raw = str(event or "")
        folded = raw.replace("/", "\\").casefold()
        if "0xc0000005" not in folded:
            continue
        # Require a path boundary so C:\Games\CR does not match C:\Games\CR-Old.
        if root_folded + "\\" not in folded:
            continue
        selected.append(raw)
    return selected[:10]


def extract_faulting_module_path(
    events: str | list[str] | tuple[str, ...]
) -> str | None:
    """Parse the full path of the faulting module, if Windows included it."""
    texts = [events] if isinstance(events, str) else list(events or [])
    for event in texts:
        text = str(event or "")
        for pattern in (
            r"Faulting module path:\s*([^\r\n]+)",
            r"Путь сбойного модуля:\s*([^\r\n]+)",
        ):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                value = match.group(1).strip().strip('"')
                return value or None
    return None


def extract_faulting_module(events: str | list[str] | tuple[str, ...]) -> str | None:
    """Parse the faulting-module name from a Windows Application Error event."""
    texts = [events] if isinstance(events, str) else list(events or [])
    for event in texts:
        text = str(event or "")
        for pattern in (
            r"Faulting module name:\s*([^,\r\n]+)",
            r"Имя сбойного модуля:\s*([^,\r\n]+)",
        ):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                name = match.group(1).strip().strip('"')
                return Path(name.replace("\\", "/")).name.casefold() or None
    module_path = extract_faulting_module_path(texts)
    if module_path:
        return Path(module_path.replace("\\", "/")).name.casefold() or None
    return None


def quarantine_faulting_optional_module(
    install_root: Path, module_name: str, module_path: str | None = None
) -> Path | None:
    """Reversibly disable ENB only when the crash report points to this local proxy."""
    root = Path(install_root).resolve()
    if Path(str(module_name or "").replace("\\", "/")).name.casefold() != "d3d9.dll":
        return None
    if not module_path:
        return None
    reported_path = Path(str(module_path).strip().strip('"').replace("\\", "/"))
    try:
        reported_resolved = reported_path.resolve()
        expected_resolved = (root / "d3d9.dll").resolve()
    except OSError:
        return None
    if os.path.normcase(str(reported_resolved)) != os.path.normcase(str(expected_resolved)):
        return None
    if not (root / "enbseries.ini").is_file():
        return None
    if not (root / "ENBSeries_License.txt").is_file():
        return None
    proxy = root / "d3d9.dll"
    backup = root / ENB_DISABLED_BACKUP
    if not proxy.is_file() or proxy.is_symlink():
        return None
    if backup.exists():
        suffix = 1
        while True:
            candidate = root / f"d3d9.dll.lederg-disabled-{suffix}.bak"
            if not candidate.exists():
                backup = candidate
                break
            suffix += 1
    try:
        proxy.replace(backup)
        return backup
    except OSError:
        return None


def sanitize_nickname(value: str) -> str:
    """Keep a game-safe nickname while allowing Latin and Cyrillic names."""
    value = re.sub(r"\s+", "_", str(value or "").strip())
    value = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_]", "", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value[:24] or "LEDERG"


def parse_server_address(value: str) -> tuple[str, int]:
    """Parse exactly one host:port endpoint from the editable GitHub IP file."""
    lines = [
        line.strip()
        for line in value.replace("\ufeff", "").splitlines()
        if line.strip() and not line.lstrip().startswith(("#", ";", "//"))
    ]
    if len(lines) != 1 or any(ch.isspace() for ch in lines[0]):
        raise ValueError("В файле IP должна быть ровно одна строка в формате host:port.")
    endpoint = lines[0]
    if endpoint.startswith("[") and "]:" in endpoint:
        host, port_text = endpoint[1:].rsplit("]:", 1)
    else:
        if endpoint.count(":") != 1:
            raise ValueError("Адрес сервера должен быть в формате host:port.")
        host, port_text = endpoint.rsplit(":", 1)
    host = host.strip().rstrip(".")
    if not host or not port_text.isdigit():
        raise ValueError("В адресе сервера неверно указаны host или port.")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("Порт сервера должен быть от 1 до 65535.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or not re.fullmatch(
            r"(?i)(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*",
            host,
        ):
            raise ValueError("Указан некорректный IP-адрес или доменное имя.")
    return host, port


def build_samp_query_packet(
    ip: str, port: int, *, opcode: str, challenge: bytes = b""
) -> bytes:
    """Build a SA:MP UDP query header for an IPv4 server endpoint."""
    parsed_ip = ipaddress.ip_address(str(ip).strip())
    if parsed_ip.version != 4:
        raise ValueError("SA:MP server queries require an IPv4 endpoint.")
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("Server port must be between 1 and 65535.")
    if not isinstance(opcode, str) or len(opcode) != 1 or not opcode.isascii():
        raise ValueError("SA:MP query opcode must be one ASCII character.")
    if opcode == "p" and len(challenge) != 4:
        raise ValueError("SA:MP ping queries require a four-byte challenge.")
    if opcode != "p" and challenge:
        raise ValueError("Only ping queries can carry a challenge.")
    return (
        b"SAMP"
        + parsed_ip.packed
        + port.to_bytes(2, "little")
        + opcode.encode("ascii")
        + challenge
    )


def query_samp_server(
    host: str,
    port: int,
    *,
    timeout: float = 0.8,
    challenge: bytes | None = None,
) -> dict:
    """Query a SA:MP server with protocol packets and return online state plus RTT.

    A valid SA:MP ping echo is preferred. If ping queries are disabled, a correctly
    framed information-query reply is accepted as proof that the query endpoint responds.
    A plain UDP send is never treated as proof of an online server.
    """
    try:
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError("Некорректный порт сервера.")
        records = socket.getaddrinfo(
            str(host).strip(), port, family=socket.AF_INET, type=socket.SOCK_DGRAM
        )
        if not records:
            raise OSError("Не удалось разрешить IPv4-адрес сервера.")
        ip = str(records[0][4][0])
        token = os.urandom(4) if challenge is None else bytes(challenge)
        if len(token) != 4:
            raise ValueError("Ping challenge must be exactly four bytes.")
        ping_packet = build_samp_query_packet(ip, port, opcode="p", challenge=token)
        info_packet = build_samp_query_packet(ip, port, opcode="i")
    except (OSError, ValueError, TypeError) as exc:
        return {
            "online": False,
            "ping_ms": None,
            "method": "none",
            "detail": f"Ошибка адреса сервера: {exc}",
        }

    udp = None
    try:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.settimeout(max(0.05, min(float(timeout), 5.0)))
        endpoint = (ip, port)

        ping_started = time.perf_counter()
        udp.sendto(ping_packet, endpoint)
        try:
            response, peer = udp.recvfrom(2048)
        except socket.timeout:
            response, peer = b"", None
        ping_elapsed = max(0, round((time.perf_counter() - ping_started) * 1000))

        if (
            peer is not None
            and peer[0] == ip
            and peer[1] == port
            and response[:11] == ping_packet[:11]
            and response[11:15] == token
            and len(response) >= 15
        ):
            return {
                "online": True,
                "ping_ms": ping_elapsed,
                "method": "ping",
                "detail": "Ответ на SA:MP ping",
            }

        info_started = time.perf_counter()
        udp.sendto(info_packet, endpoint)
        try:
            response, peer = udp.recvfrom(4096)
        except socket.timeout:
            response, peer = b"", None
        info_elapsed = max(0, round((time.perf_counter() - info_started) * 1000))
        if (
            peer is not None
            and peer[0] == ip
            and peer[1] == port
            and response[:11] == info_packet[:11]
            and len(response) > 11
        ):
            return {
                "online": True,
                "ping_ms": info_elapsed,
                "method": "info",
                "detail": "Ответ на SA:MP info",
            }
        return {
            "online": False,
            "ping_ms": None,
            "method": "none",
            "detail": "Нет ответа на SA:MP query",
        }
    except (OSError, ValueError, TypeError) as exc:
        return {
            "online": False,
            "ping_ms": None,
            "method": "none",
            "detail": f"Ошибка UDP-запроса: {exc}",
        }
    finally:
        if udp is not None:
            try:
                udp.close()
            except OSError:
                pass


def format_client_exit_code(code: int) -> str:
    """Translate common Windows process failure codes into actionable diagnostics."""
    unsigned = int(code) & 0xFFFFFFFF
    if unsigned == 0xC0000005:
        return (
            f"CR:MP завершился с кодом {int(code)} (0x{unsigned:08X}: "
            "ACCESS_VIOLATION — нарушение доступа к памяти). Это сбой игрового "
            "клиента или одной из его DLL; сам код не указывает виновный файл. "
            "Нажми «Переустановить» для восстановления файлов. Если повторится, "
            "нужен журнал Windows «Приложение» с именем сбойного модуля."
        )
    return f"Клиент завершился с кодом {code} (0x{unsigned:08X})."


def load_server_address(cache_path: Path, fetcher=None) -> tuple[str, int]:
    """Refresh server endpoint from GitHub; use last known good cache offline."""
    fetcher = fetcher or HTTP.get
    try:
        response = fetcher(SERVER_IP_URL, timeout=(4, 8))
        response.raise_for_status()
        endpoint = parse_server_address(response.text)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(f"{endpoint[0]}:{endpoint[1]}", encoding="utf-8")
        return endpoint
    except Exception as exc:
        if cache_path.is_file():
            try:
                return parse_server_address(cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        # Known endpoint from the current public config; keep Play usable when
        # GitHub is temporarily unreachable on a first launch.
        return SERVER_HOST, SERVER_PORT


def fetch_release_manifest(fetcher=None) -> dict:
    """Fetch and validate a release manifest so client updates need no EXE rebuild."""
    fetcher = fetcher or HTTP.get
    response = fetcher(CLIENT_MANIFEST_URL, timeout=(4, 12))
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("schema", 1) != 1:
        raise ValueError("Неподдерживаемый формат манифеста клиента.")
    version = str(payload.get("version", "")).strip()
    archive_url = str(payload.get("archive_url", "")).strip()
    files_manifest_url = str(payload.get("files_manifest_url", "")).strip()
    if not version or not archive_url:
        raise ValueError("В манифесте отсутствует version или archive_url.")
    parsed = urlparse(archive_url)
    if not parsed.scheme:
        from urllib.parse import urljoin
        manifest_directory = CLIENT_MANIFEST_URL.rsplit("/", 1)[0] + "/"
        archive_url = urljoin(manifest_directory, archive_url.lstrip("/"))
    elif parsed.scheme.lower() not in {"https", "http"}:
        raise ValueError("URL архива должен использовать HTTP или HTTPS.")
    elif parsed.scheme.lower() == "http" and not (parsed.hostname or "").lower().endswith(".cloudpub.ru"):
        raise ValueError("Незашифрованный HTTP разрешён только для домена Cloudpub.")
    if not files_manifest_url:
        # Keep older client.json files compatible with the current manifest-first updater.
        files_manifest_url = CLIENT_FILES_MANIFEST_URL
    if files_manifest_url:
        files_parsed = urlparse(files_manifest_url)
        if not files_parsed.scheme:
            from urllib.parse import urljoin
            files_manifest_url = urljoin(
                CLIENT_MANIFEST_URL.rsplit("/", 1)[0] + "/",
                files_manifest_url.lstrip("/"),
            )
        elif files_parsed.scheme.lower() not in {"https", "http"}:
            raise ValueError("URL манифеста файлов должен использовать HTTP или HTTPS.")
        elif files_parsed.scheme.lower() == "http" and not (
            (files_parsed.hostname or "").lower().endswith(".cloudpub.ru")
        ):
            raise ValueError("Незашифрованный URL манифеста разрешён только для Cloudpub.")
    size = int(payload.get("archive_size", 0) or 0)
    if size < 0:
        raise ValueError("Некорректный размер архива в манифесте.")
    digest = str(payload.get("sha256", "") or "").lower().strip()
    if digest and not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Некорректный SHA-256 в манифесте.")
    executable = str(payload.get("client_executable", DEFAULT_CLIENT_EXECUTABLE)).strip()
    if not executable or Path(executable).is_absolute() or ".." in Path(executable).parts:
        raise ValueError("Некорректный путь к исполняемому файлу клиента.")
    manifest_mode = str(payload.get("launch_mode", DEFAULT_LAUNCH_MODE)).strip().lower()
    if manifest_mode not in {"direct", "samp-cli", "browser", "browser-ui"}:
        raise ValueError("Неизвестный launch_mode в манифесте.")
    # A stale public manifest must not switch the client back to browser-driven launch.
    # Keep archive metadata as a compatibility fallback; prefer the per-file manifest.
    mode = DEFAULT_LAUNCH_MODE
    payload.update({
        "version": version,
        # Keep the archive fallback pinned to the known Drive client; its metadata
        # must not be inherited from an arbitrary/old public manifest.
        "archive_url": DRIVE_DOWNLOAD_URL,
        "files_manifest_url": files_manifest_url,
        "archive_size": 0,
        "sha256": "",
        "client_executable": executable.replace("\\", "/"),
        "launch_mode": mode,
    })
    return payload


def find_client(root: Path, preferred_executable: str | None = None) -> Path | None:
    if not root.exists() or not root.is_dir():
        return None
    if preferred_executable:
        relative = Path(preferred_executable)
        if not relative.is_absolute() and ".." not in relative.parts:
            direct = root / relative
            if direct.is_file():
                return direct
            # Archives may have a product-named top-level directory.
            matches = [
                candidate for candidate in root.rglob(relative.name)
                if candidate.is_file()
                and tuple(p.lower() for p in candidate.parts[-len(relative.parts):])
                    == tuple(p.lower() for p in relative.parts)
            ]
            if matches:
                return sorted(matches, key=lambda p: len(p.parts))[0]
    names = {"multiplayer_browser_cr.exe", "crmp.exe", "samp.exe"}
    priority = {"multiplayer_browser_cr.exe": 0, "crmp.exe": 1, "samp.exe": 2}
    found: list[Path] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [
            d for d in dirs
            if d.lower() not in {"$recycle.bin", "system volume information", "__macosx"}
        ]
        for name in files:
            if name.lower() in names:
                found.append(Path(current) / name)
    found.sort(key=lambda p: (priority[p.name.lower()], len(p.parts)))
    return found[0] if found else None


def game_root_for(executable: Path) -> Path:
    for parent in (executable.parent, *executable.parents):
        if (parent / "gta_sa.exe").is_file():
            return parent
    return executable.parent


def find_child_game_pid(parent_pid: int) -> int | None:
    """Find the game process spawned by the CR:MP command-line launcher."""
    if os.name != "nt" or not parent_pid:
        return None

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = ctypes.c_int
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if (
                entry.szExeFile.lower() in {
                    "grand_theft_auto_san_andreas.dll",
                    "grand_theft_auto_san_andreas.exe",
                    "gta_sa.exe",
                }
                and (
                    entry.th32ProcessID == int(parent_pid)
                    or entry.th32ParentProcessID == int(parent_pid)
                )
            ):
                return int(entry.th32ProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return None


def is_process_running(pid: int | None) -> bool:
    """Return whether a Windows process ID still refers to a running process."""
    if os.name != "nt" or not pid:
        return False
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x1000, 0, int(pid))
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        return bool(
            kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            and exit_code.value == 259
        )
    finally:
        kernel32.CloseHandle(handle)


def build_launch_command(
    executable: Path,
    host: str,
    port: int,
    nickname: str = "Player",
    launch_mode: str = DEFAULT_LAUNCH_MODE,
) -> list[str]:
    """Build the client launch contract; the actual game client owns the UDP socket."""
    if launch_mode in {"browser", "browser-ui"}:
        return [str(executable)]
    nick = sanitize_nickname(nickname)
    return [str(executable), f"{host}:{int(port)}", "-n", nick]



def direct_launcher_path() -> Path:
    """Locate the bundled x86 helper in development and PyInstaller one-file builds."""
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_root / DIRECT_LAUNCHER_NAME


def build_direct_launcher_command(
    helper: Path,
    game_root: Path,
    host: str,
    port: int,
    nickname: str = "LEDERG",
) -> list[str]:
    """Build an explicit browser-free launch request for the x86 injector helper."""
    host = host.strip()
    if not host or any(ch.isspace() for ch in host) or any(ch in host for ch in '"\\/'):
        raise ValueError("Некорректный адрес сервера.")
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("Порт сервера должен быть от 1 до 65535.")
    root = Path(game_root)
    game = root / "gta_sa.exe"
    multiplayer_dll = find_injected_multiplayer_dll(root)
    if not game.is_file():
        raise FileNotFoundError(f"Не найден игровой файл: {game}")
    if multiplayer_dll is None:
        raise FileNotFoundError(
            "Не найдена multiplayer DLL san_andreas_multiplayer.dll в папке клиента."
        )
    nick = sanitize_nickname(nickname)
    return [
        str(helper),
        "--game", str(game),
        "--dll", str(multiplayer_dll),
        "--host", host,
        "--port", str(port),
        "--nickname", nick,
    ]



def spawn_direct_launcher(command: list[str], game_root: Path) -> subprocess.Popen:
    """Run the bundled direct-connect helper with no visible console window."""
    options = {
        "cwd": str(Path(game_root).resolve()),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = subprocess.SW_HIDE
        options["startupinfo"] = startup_info
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(command, **options)


def find_direct_game_client(root: Path) -> Path | None:
    """Find the CR:MP runtime binary shipped with the client."""
    candidates = (
        root / "multiplayer_c" / "grand_theft_auto_san_andreas.dll",
        root / "grand_theft_auto_san_andreas.dll",
        root / "multiplayer_c" / "grand_theft_auto_san_andreas.exe",
        root / "grand_theft_auto_san_andreas.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def find_injected_multiplayer_dll(root: Path) -> Path | None:
    """Find the multiplayer DLL loaded into gta_sa.exe by the direct-connect helper."""
    candidates = (
        root / "multiplayer_c" / "san_andreas_multiplayer.dll",
        root / "san_andreas_multiplayer.dll",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def build_direct_game_command(
    executable: Path,
    host: str,
    port: int,
    nickname: str = "LEDERG",
) -> list[str]:
    """Build the same native command line used by the CR:MP browser."""
    host_input = str(host or "").strip()
    if ":" in host_input and not host_input.startswith("["):
        endpoint = f"[{host_input}]:{port}"
    else:
        endpoint = f"{host_input}:{port}"
    validated_host, validated_port = parse_server_address(endpoint)
    return [
        str(executable),
        "-c",
        "-n", sanitize_nickname(nickname),
        "-h", validated_host,
        "-p", str(validated_port),
    ]


def write_launcher_log(message: str) -> None:
    """Append a timestamped launch diagnostic outside the installation folder."""
    try:
        base = Path(
            os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        ) / "LEDERG CRMP"
        base.mkdir(parents=True, exist_ok=True)
        with (base / "launcher.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def connect_via_browser_ui(
    executable: Path, host: str, port: int, nickname: str = "LEDERG"
) -> subprocess.Popen:
    """Open the official CR:MP browser and connect through its server UI."""
    host_text = str(host or "").strip()
    if ":" in host_text and not host_text.startswith("["):
        host_text = f"[{host_text}]"
    endpoint_host, endpoint_port = parse_server_address(f"{host_text}:{int(port)}")
    if executable.name.casefold() != "multiplayer_browser_cr.exe":
        raise ValueError(
            "Для запуска через браузер нужен multiplayer_c/multiplayer_browser_cr.exe."
        )
    game_root = game_root_for(executable)
    if os.name != "nt":
        # Keeps discovery/unit tests portable; real UI automation is Windows-only.
        return subprocess.Popen([str(executable)], cwd=str(game_root))

    user32 = ctypes.windll.user32
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    def windows(pid: int):
        found = []

        def callback(hwnd, _):
            process_id = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if process_id.value == pid:
                n = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                rect = RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                found.append((hwnd, buf.value, bool(user32.IsWindowVisible(hwnd)), rect))
            return True

        user32.EnumWindows(EnumProc(callback), 0)
        return found

    def click(x: int, y: int, double: bool = False):
        user32.SetCursorPos(int(x), int(y))
        time.sleep(.12)
        for _ in range(2 if double else 1):
            user32.mouse_event(2, 0, 0, 0, 0)
            user32.mouse_event(4, 0, 0, 0, 0)
            time.sleep(.16)

    def click_relative(hwnd, x, y, double=False):
        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        click(rect.left + x, rect.top + y, double)

    def find_window(pid: int, needles, visible=True):
        if isinstance(needles, str):
            needles = (needles,)
        needles = tuple(str(value).casefold() for value in needles)
        for hwnd, title, shown, rect in windows(pid):
            if any(needle in title.casefold() for needle in needles) and (
                visible is None or shown == visible
            ):
                return hwnd, title, rect
        return None

    def wait_window(pid: int, needles, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            item = find_window(pid, needles, True)
            if item:
                return item
            time.sleep(.15)
        return None

    def type_text(value):
        KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x0004, 0x0002

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", ctypes.c_ushort),
                ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class INPUTUNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ulong), ("u", INPUTUNION)]

        for char in value:
            event = INPUT(
                1,
                INPUTUNION(KEYBDINPUT(0, ord(char), KEYEVENTF_UNICODE, 0, None)),
            )
            user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
            event.u.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
            user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))

    def press_key(key):
        user32.keybd_event(key, 0, 0, 0)
        time.sleep(.04)
        user32.keybd_event(key, 0, 2, 0)

    def press_enter():
        press_key(0x0D)

    def replace_focused_text(value):
        # Ctrl+A avoids appending to an old address or nickname left in the edit box.
        user32.keybd_event(0x11, 0, 0, 0)
        user32.keybd_event(0x41, 0, 0, 0)
        time.sleep(.04)
        user32.keybd_event(0x41, 0, 2, 0)
        user32.keybd_event(0x11, 0, 2, 0)
        time.sleep(.05)
        type_text(value)

    process = subprocess.Popen(
        [str(executable)],
        cwd=str(game_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    write_launcher_log(
        f"CR:MP browser launch requested: executable={executable}; cwd={game_root}; "
        f"endpoint={endpoint_host}:{endpoint_port}; nickname={nickname}"
    )
    try:
        browser = wait_window(
            process.pid,
            ("Criminal Russia Multiplayer", "Criminal Russia MP"),
            15,
        )
        if not browser:
            raise RuntimeError(
                "Запустил multiplayer_browser_cr.exe, но окно браузера CR:MP не появилось. "
                "Проверь файлы в multiplayer_c и открой «ЖУРНАЛ»."
            )
        hwnd = browser[0]
        user32.SetForegroundWindow(hwnd)
        time.sleep(.5)

        # The client's toolbar contains Add Server. Dialog titles differ between languages.
        click_relative(hwnd, 49, 64)
        add_dialog = wait_window(
            process.pid,
            ("Добавление сервера", "Добавить сервер", "Add Server", "Add a Server"),
            4,
        )
        if not add_dialog:
            raise RuntimeError(
                "Браузер открыт, но окно добавления сервера не найдено. "
                "Если интерфейс клиента отличается, открой «ЖУРНАЛ» для диагностики."
            )
        user32.SetForegroundWindow(add_dialog[0])
        replace_focused_text(f"{endpoint_host}:{endpoint_port}")
        press_enter()
        time.sleep(1.5)

        # This client archive starts with an empty server list. The just-added endpoint
        # is therefore the first row; double-click it to open its properties.
        click_relative(hwnd, 180, 106, double=True)
        props = wait_window(
            process.pid,
            ("Свойства сервера", "Server Properties", "Server properties"),
            4,
        )
        if not props:
            raise RuntimeError(
                f"Адрес {endpoint_host}:{endpoint_port} добавлен в браузер, но окно "
                "свойств сервера не открылось. Проверь окно CR:MP и журнал лаунчера."
            )
        user32.SetForegroundWindow(props[0])
        click_relative(props[0], 62, 205)

        nick_dialog = wait_window(
            process.pid,
            ("Кто вы", "Who are you", "Enter your nickname", "Nickname"),
            3,
        )
        if nick_dialog:
            user32.SetForegroundWindow(nick_dialog[0])
            click_relative(nick_dialog[0], 140, 67)
            clean_nickname = re.sub(r"[^A-Za-z0-9_]", "", nickname)[:24] or "LEDERG"
            replace_focused_text(clean_nickname)
            press_enter()
            time.sleep(.8)
            # On some client builds the nickname dialog returns to server properties;
            # press Connect a second time to finish the handoff.
            props_again = wait_window(
                process.pid,
                ("Свойства сервера", "Server Properties", "Server properties"),
                1.5,
            )
            if props_again:
                user32.SetForegroundWindow(props_again[0])
                click_relative(props_again[0], 62, 205)
        else:
            # A saved nickname may skip the prompt, but a still-open properties window
            # indicates Connect did not proceed.
            props_again = wait_window(
                process.pid,
                ("Свойства сервера", "Server Properties", "Server properties"),
                1,
            )
            if props_again:
                raise RuntimeError(
                    "В окне свойств сервера не открылся экран ника/подключения. "
                    "Проверь раскладку окон клиента и журнал лаунчера."
                )

        write_launcher_log(
            f"CR:MP browser UI connection flow submitted for {endpoint_host}:{endpoint_port}; "
            f"pid={process.pid}"
        )
        return process
    except Exception as exc:
        write_launcher_log(
            f"CR:MP browser UI connection failed: endpoint={endpoint_host}:{endpoint_port}; "
            f"pid={process.pid}; error={exc!r}"
        )
        # Avoid leaving an untracked browser hidden behind the launcher when the UI
        # flow could not reach the server connection stage.
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        raise



def safe_extract_zip(
    archive: Path,
    destination: Path,
    progress=None,
    cancel=None,
    *,
    integrity_manifest_path: Path | None = None,
) -> int:
    """Safely extract ZIP entries and optionally record SHA-256 baselines while streaming."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    extracted_files = 0
    integrity_entries: list[dict] = []

    with zipfile.ZipFile(archive, "r") as bundle:
        entries = bundle.infolist()
        total_bytes = sum(entry.file_size for entry in entries if not entry.is_dir())
        free = shutil.disk_usage(destination).free
        required = total_bytes + 128 * 1024 * 1024
        if free < required:
            need_gb = required / (1024 ** 3)
            free_gb = free / (1024 ** 3)
            raise RuntimeError(
                "Недостаточно свободного места для распаковки.\n\n"
                f"Нужно примерно {need_gb:.1f} ГБ свободного места, "
                f"сейчас доступно {free_gb:.1f} ГБ.\n"
                "Архив сохранён — освободи место и запусти установку повторно."
            )

        completed_bytes = 0
        for entry in entries:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Установка остановлена. Архив сохранён, можно продолжить позже.")
            name = entry.filename.replace("\\", "/")
            relative = PurePosixPath(name)
            if (
                relative.is_absolute()
                or name.startswith("/")
                or (relative.parts and ":" in relative.parts[0])
                or any(part in ("..", "") for part in relative.parts)
            ):
                raise ValueError(f"В ZIP обнаружен небезопасный путь: {entry.filename}")

            target = (destination / Path(*relative.parts)).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"Путь выходит за пределы выбранной папки: {entry.filename}") from exc

            mode = (entry.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise ValueError(f"Символьные ссылки в архиве не поддерживаются: {entry.filename}")

            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256() if integrity_manifest_path is not None else None
            with bundle.open(entry, "r") as source, target.open("wb") as output:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError(
                            "Распаковка остановлена. Архив сохранён; нажми установку, чтобы повторить."
                        )
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    if digest is not None:
                        digest.update(chunk)
                    completed_bytes += len(chunk)
                    if progress:
                        progress(completed_bytes, total_bytes, entry.filename)
            extracted_files += 1
            if digest is not None:
                info = target.stat()
                integrity_entries.append({
                    "path": relative.as_posix(),
                    "size": info.st_size,
                    "sha256": digest.hexdigest(),
                    "crc32": f"{int(entry.CRC) & 0xFFFFFFFF:08x}",
                    "mtime_ns": info.st_mtime_ns,
                })

    if integrity_manifest_path is not None:
        integrity_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 1,
            "source": "google-drive-archive",
            "created_at": int(time.time()),
            "files": integrity_entries,
        }
        temporary = integrity_manifest_path.with_name(integrity_manifest_path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, integrity_manifest_path)

    return extracted_files


def _content_range_total(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"bytes\s+\d+-\d+/(\d+)", value, re.I)
    return int(match.group(1)) if match else None


def _is_google_drive_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    query = parse_qs(parsed.query)
    return bool(
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() in {
            "drive.usercontent.google.com",
            "drive.google.com",
        }
        and parsed.path in {"/download", "/uc"}
        and query.get("id", [""])[0] == DRIVE_FILE_ID
    )


def _validate_client_archive_source(url: str) -> str:
    """Reject all client archive sources except the one user-supplied Drive file."""
    if not _is_google_drive_url(url):
        raise ValueError("Игровой клиент разрешено скачивать только из указанного Google Drive.")
    return url


def _google_drive_confirmation_url(
    response_url: str, html: str, cookies: dict[str, str] | None = None
) -> str:
    """Resolve Google's large-file virus-scan confirmation page without downloading its HTML."""
    cookies = cookies or {}
    # Prefer Google's confirmation form because it may carry additional UUID parameters.
    form = re.search(
        r"<form\b([^>]*)>(.*?)</form\s*>", html, flags=re.I | re.S
    )
    if form:
        attrs = {
            key.lower(): unescape(value1 if value1 is not None else value2 if value2 is not None else value3 or "")
            for key, value1, value2, value3 in re.findall(
                r"([\w:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
                form.group(1),
                flags=re.I,
            )
        }
        action = attrs.get("action")
        fields = {}
        for tag in re.findall(r"<input\b([^>]*)>", form.group(2), flags=re.I | re.S):
            values = {
                key.lower(): unescape(v1 if v1 is not None else v2 if v2 is not None else v3 or "")
                for key, v1, v2, v3 in re.findall(
                    r"([\w:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
                    tag,
                    flags=re.I,
                )
            }
            if values.get("name") and "value" in values:
                fields[values["name"]] = values["value"]
        if fields.get("confirm"):
            target = urljoin(response_url, action or response_url)
            parts = urlparse(target)
            query = parse_qs(parts.query, keep_blank_values=True)
            query.update({key: [value] for key, value in fields.items()})
            confirmed = urlunparse(parts._replace(query=urlencode(query, doseq=True)))
            return _validate_client_archive_source(confirmed)

    token = ""
    for name, value in cookies.items():
        if name.lower().startswith("download_warning") and value:
            token = value
            break
    if not token:
        for pattern in (
            r"name\s*=\s*[\"']confirm[\"'][^>]*value\s*=\s*[\"']([^\"']+)",
            r"value\s*=\s*[\"']([^\"']+)[\"'][^>]*name\s*=\s*[\"']confirm[\"']",
            r"[?&]confirm=([A-Za-z0-9_-]+)",
        ):
            match = re.search(pattern, html, flags=re.I)
            if match:
                token = unescape(match.group(1))
                break
    parts = urlparse(response_url)
    query = parse_qs(parts.query, keep_blank_values=True)
    if token:
        query["confirm"] = [token]
    elif query.get("confirm"):
        return _validate_client_archive_source(response_url)
    else:
        # Google Drive sometimes omits both the warning cookie and a usable
        # confirmation form for large shared files. Its web flow also accepts
        # confirm=t; try that before failing the download.
        query["confirm"] = ["t"]
    confirmed = urlunparse(parts._replace(query=urlencode(query, doseq=True)))
    return _validate_client_archive_source(confirmed)


def _open_google_drive_response(
    url: str, headers: dict[str, str] | None = None, timeout=(20, 60)
):
    """Open the pinned Drive file, automatically handling the large-file confirmation page."""
    _validate_client_archive_source(url)
    request_headers = headers or {}
    response = HTTP.get(
        url,
        headers=request_headers,
        stream=True,
        allow_redirects=True,
        timeout=timeout,
    )
    content_type = (response.headers.get("Content-Type") or "").lower()
    # Some Google front ends incorrectly label the confirmation page as binary.
    # Inspect the first streamed bytes as well as Content-Type before treating it as an archive.
    probe_stream = response.iter_content(chunk_size=64 * 1024)
    first_chunk = next(probe_stream, b"")
    looks_like_html = first_chunk.lstrip().lower().startswith((b"<!doctype html", b"<html"))
    if "text/html" not in content_type and not looks_like_html:
        response._lederg_prefetched_chunk = first_chunk
        return response, url
    body = bytearray(first_chunk)
    for chunk in probe_stream:
        if chunk:
            body.extend(chunk)
            if len(body) > 2 * 1024 * 1024:
                response.close()
                raise RuntimeError("Google Drive вернул слишком большую HTML-страницу вместо подтверждения.")
    response_url = response.url
    cookies = response.cookies.get_dict()
    response.close()
    confirmed_url = _google_drive_confirmation_url(
        response_url, body.decode("utf-8", errors="replace"), cookies
    )
    confirmed = HTTP.get(
        confirmed_url,
        headers=request_headers,
        stream=True,
        allow_redirects=True,
        timeout=timeout,
    )
    confirmed_type = (confirmed.headers.get("Content-Type") or "").lower()
    confirm_probe = confirmed.iter_content(chunk_size=64 * 1024)
    confirm_first = next(confirm_probe, b"")
    confirm_html = confirm_first.lstrip().lower().startswith((b"<!doctype html", b"<html"))
    if "text/html" in confirmed_type or confirm_html:
        confirmed.close()
        raise RuntimeError(
            "Google Drive повторно вернул HTML вместо ZIP. Проверь доступ «Все, у кого есть ссылка» "
            "и наличие файла по ссылке клиента; HTML не сохраняется как архив."
        )
    confirmed._lederg_prefetched_chunk = confirm_first
    return confirmed, confirmed_url


def download_archive(destination: Path, progress=None, cancel=None, url: str = ZIP_URL,
                     expected_sha256: str = "", expected_size: int = 0) -> Path:
    """
    Download a ZIP from the LEDERG HTTP endpoint. Supports Range resume,
    validates server responses, and keeps partial files after interruptions.
    """
    _validate_client_archive_source(url)
    destination.mkdir(parents=True, exist_ok=True)
    # Keep this file separate from archives downloaded by older builds from Cloudpub.
    partial = destination / ".lederg-crmp-google-drive.zip.part"
    complete = destination / ".lederg-crmp-google-drive.zip"

    def archive_matches(candidate: Path) -> bool:
        if not candidate.is_file() or not zipfile.is_zipfile(candidate):
            return False
        if expected_size and candidate.stat().st_size != expected_size:
            return False
        if expected_sha256:
            digest = hashlib.sha256()
            with candidate.open("rb") as stream:
                for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest().lower() != expected_sha256.lower():
                return False
        return True

    if complete.is_file() and archive_matches(complete):
        size = complete.stat().st_size
        if progress:
            progress("download", size, size, "Архив проверен — использую готовый файл")
        return complete
    if complete.exists():
        complete.unlink(missing_ok=True)

    if complete.exists():
        complete.unlink(missing_ok=True)

    if partial.is_file() and archive_matches(partial):
        partial.replace(complete)
        size = complete.stat().st_size
        if progress:
            progress("download", size, size, "Готовый архив восстановлен")
        return complete

    _validate_client_archive_source(url)
    try:
        probe, resolved_url = _open_google_drive_response(
            url, headers={}, timeout=(20, 45)
        )
        try:
            probe.raise_for_status()
            remote_size = int(probe.headers.get("Content-Length") or 0)
            if probe.status_code == 206:
                remote_size = _content_range_total(probe.headers.get("Content-Range")) or remote_size
            etag = probe.headers.get("ETag")
        finally:
            probe.close()
        url = resolved_url
    except Exception as exc:
        raise RuntimeError(
            "Не удалось подключиться к указанному архиву Google Drive. "
            "Проверь интернет-соединение и доступ «Все, у кого есть ссылка».\n\n"
            f"Детали: {exc}"
        ) from exc

    if expected_size:
        if remote_size and remote_size != expected_size:
            raise RuntimeError(
                f"Размер архива на сервере не совпадает с манифестом: "
                f"{remote_size:,} вместо {expected_size:,} байт."
            )
        remote_size = expected_size
    if remote_size and partial.is_file() and partial.stat().st_size > remote_size:
        partial.unlink(missing_ok=True)

    free = shutil.disk_usage(destination).free
    required = remote_size or ZIP_SIZE
    if not partial.exists() and required and free < required + 256 * 1024 * 1024:
        need_gb = (required + 256 * 1024 * 1024) / (1024 ** 3)
        free_gb = free / (1024 ** 3)
        raise RuntimeError(
            "На выбранном диске недостаточно свободного места для архива.\n\n"
            f"Нужно примерно {need_gb:.1f} ГБ, доступно {free_gb:.1f} ГБ."
        )

    last_error: Exception | None = None
    for attempt in range(4):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Загрузка приостановлена. Частичный файл сохранён.")

        offset = partial.stat().st_size if partial.exists() else 0
        headers = {}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            if etag:
                headers["If-Range"] = etag

        try:
            response, resolved_url = _open_google_drive_response(
                url, headers=headers, timeout=(20, 90)
            )
            url = resolved_url
            with response:
                if offset and response.status_code == 416:
                    if (
                        (not remote_size or partial.stat().st_size == remote_size)
                        and archive_matches(partial)
                    ):
                        partial.replace(complete)
                        return complete
                    partial.unlink(missing_ok=True)
                    last_error = RuntimeError(
                        "Сервер не принял продолжение загрузки; повторяю с начала."
                    )
                    continue

                if offset and response.status_code == 200:
                    # Range ignored or ETag changed: restart to prevent a mixed archive.
                    offset = 0
                    remote_size = int(response.headers.get("Content-Length") or remote_size)
                elif offset and response.status_code != 206:
                    response.raise_for_status()
                    raise RuntimeError(
                        f"Неожиданный HTTP-код при продолжении загрузки: {response.status_code}"
                    )
                else:
                    response.raise_for_status()

                range_value = response.headers.get("Content-Range")
                if response.status_code == 206:
                    match = re.search(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", range_value or "", re.I)
                    if not match or int(match.group(1)) != offset:
                        partial.unlink(missing_ok=True)
                        last_error = RuntimeError("Сервер вернул неверный Content-Range.")
                        continue
                    range_total = int(match.group(3)) if match.group(3) != "*" else 0
                    if remote_size and range_total and range_total != remote_size:
                        partial.unlink(missing_ok=True)
                        last_error = RuntimeError(
                            "Размер архива изменился во время загрузки; повторяю с начала."
                        )
                        continue
                    remote_size = range_total or remote_size
                else:
                    remote_size = int(response.headers.get("Content-Length") or remote_size)

                content_type = (response.headers.get("Content-Type") or "").lower()
                first_chunk = offset == 0
                received = offset
                mode = "ab" if offset else "wb"
                with partial.open(mode) as output:
                    prefetched = getattr(response, "_lederg_prefetched_chunk", b"")
                    stream_chunks = response.iter_content(chunk_size=1024 * 1024)
                    if prefetched:
                        stream_chunks = itertools.chain((prefetched,), stream_chunks)
                    for chunk in stream_chunks:
                        if cancel is not None and cancel.is_set():
                            output.flush()
                            raise InterruptedError(
                                "Загрузка приостановлена. Нажми установку повторно, чтобы продолжить."
                            )
                        if not chunk:
                            continue
                        if first_chunk:
                            first_chunk = False
                            prefix = chunk.lstrip().lower()
                            if (
                                "text/html" in content_type
                                or prefix.startswith((b"<!doctype html", b"<html"))
                            ):
                                raise RuntimeError(
                                    "Сервер вернул HTML-страницу вместо ZIP-архива."
                                )
                            if not chunk.startswith(b"PK"):
                                raise RuntimeError("Ответ сервера не похож на ZIP-архив.")
                        output.write(chunk)
                        received += len(chunk)
                        if progress:
                            progress(
                                "download",
                                received,
                                remote_size or 0,
                                "GTA Criminal Russia.zip",
                            )
                break
        except InterruptedError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= 3:
                break
            time.sleep(0.8 * (attempt + 1))

    if not partial.is_file() or partial.stat().st_size == 0:
        raise RuntimeError(f"Архив не скачан: {last_error or 'пустой ответ сервера'}")

    actual_size = partial.stat().st_size
    if remote_size and actual_size != remote_size:
        raise RuntimeError(
            "Загрузка оборвалась раньше времени; прогресс сохранён. "
            f"Получено {actual_size:,} из {remote_size:,} байт. "
            "Нажми установку повторно, чтобы продолжить."
        )
    if not zipfile.is_zipfile(partial):
        raise RuntimeError(
            "Скачанный файл не является целым ZIP-архивом. "
            "Частичный файл сохранён; повтори загрузку."
        )
    if expected_sha256:
        digest = hashlib.sha256()
        with partial.open("rb") as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        actual_digest = digest.hexdigest().lower()
        if actual_digest != expected_sha256.lower():
            raise RuntimeError(
                "SHA-256 архива не совпал с манифестом. Архив оставлен для диагностики; "
                "обнови манифест или пересобери архив."
            )

    partial.replace(complete)
    return complete


def _safe_client_relative_path(value: str) -> PurePosixPath:
    """Validate a manifest path before combining it with the install directory."""
    name = str(value or "").replace("\\", "/")
    relative = PurePosixPath(name)
    if (
        not name
        or not relative.parts
        or relative.is_absolute()
        or name.startswith("/")
        or (relative.parts and ":" in relative.parts[0])
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise ValueError(f"В манифесте клиента указан небезопасный путь: {value!r}")
    return relative


def _file_crc32(path: Path) -> int:
    checksum = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum = zlib.crc32(chunk, checksum)
    return checksum & 0xFFFFFFFF


def download_client_folder(
    destination: Path,
    manifest_url: str,
    progress=None,
    cancel=None,
    http=None,
    manifest_payload: dict | None = None,
) -> tuple[Path, int]:
    """
    Download a client as individual files into its install folder.

    A manifest supplies safe relative paths, expected sizes, and CRC32 values.
    Each file uses its own temporary .part file and can resume with HTTP Range.
    """
    client_http = http or HTTP
    try:
        payload = (
            fetch_files_manifest(manifest_url, http=client_http)
            if manifest_payload is None
            else validate_manifest(manifest_payload)
        )
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Не удалось получить манифест файлов клиента: {exc}") from exc

    install_root_name = payload["install_root"]
    root_rel = PurePosixPath(install_root_name)
    base_url = payload["files_base_url"]
    files = [
        (PurePosixPath(item["path"]), item["size"], int(item["crc32"], 16))
        for item in payload["files"]
    ]
    total_bytes = payload["total_bytes"]
    if total_bytes <= 0:
        raise ValueError("Манифест клиента не содержит файлов для загрузки.")

    destination.mkdir(parents=True, exist_ok=True)
    install_root = (destination / root_rel.parts[0]).resolve()
    destination_root = destination.resolve()
    try:
        install_root.relative_to(destination_root)
    except ValueError as exc:
        raise ValueError("Папка установки выходит за пределы выбранного места.") from exc
    install_root.mkdir(parents=True, exist_ok=True)

    completed_bytes = 0
    for relative, expected_size, expected_crc in files:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Загрузка остановлена. Уже скачанные файлы сохранены.")

        target = install_root.joinpath(*relative.parts)
        try:
            # Resolve before creating parent directories so symlinks inside the
            # install tree cannot cause writes outside the selected directory.
            resolved_target = target.resolve()
            resolved_target.relative_to(install_root)
        except ValueError as exc:
            raise ValueError(f"Файл выходит за пределы папки установки: {relative.as_posix()}") from exc
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.is_file() and target.stat().st_size == expected_size:
            if _file_crc32(target) == expected_crc:
                completed_bytes += expected_size
                if progress:
                    progress("download", completed_bytes, total_bytes, relative.as_posix())
                continue

        part = target.with_name(target.name + ".lederg.part")
        if part.is_file() and part.stat().st_size == expected_size:
            if _file_crc32(part) == expected_crc:
                part.replace(target)
                completed_bytes += expected_size
                if progress:
                    progress("download", completed_bytes, total_bytes, relative.as_posix())
                continue
        if part.is_file() and part.stat().st_size > expected_size:
            part.unlink()

        quoted_path = "/".join(quote(segment, safe="") for segment in relative.parts)
        file_url = base_url.rstrip("/") + "/" + quoted_path
        last_error = None

        for attempt in range(4):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Загрузка остановлена. Частичный файл сохранён для продолжения.")

            offset = part.stat().st_size if part.is_file() else 0
            if offset > expected_size:
                part.unlink(missing_ok=True)
                offset = 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            checksum = _file_crc32(part) if offset else 0

            try:
                with client_http.get(
                    file_url, headers=headers, stream=True, timeout=(20, 60)
                ) as file_response:
                    if offset and file_response.status_code == 206:
                        match = re.fullmatch(
                            r"bytes\s+(\d+)-(\d+)/(\d+)",
                            file_response.headers.get("Content-Range", ""), re.I
                        )
                        if not match or int(match.group(1)) != offset or int(match.group(3)) != expected_size:
                            raise RuntimeError(f"Сервер вернул некорректный Range для {relative.as_posix()}")
                        write_mode = "ab"
                    else:
                        if file_response.status_code not in (200, 206):
                            file_response.raise_for_status()
                        if file_response.status_code == 206:
                            match = re.fullmatch(
                                r"bytes\s+(\d+)-(\d+)/(\d+)",
                                file_response.headers.get("Content-Range", ""), re.I
                            )
                            if not match or int(match.group(1)) != 0 or int(match.group(3)) != expected_size:
                                raise RuntimeError(f"Сервер вернул некорректный Range для {relative.as_posix()}")
                        offset = 0
                        checksum = 0
                        write_mode = "wb"

                    received = offset
                    with part.open(write_mode) as output:
                        for chunk in file_response.iter_content(chunk_size=1024 * 1024):
                            if cancel is not None and cancel.is_set():
                                raise InterruptedError(
                                    "Загрузка остановлена. Частичный файл сохранён для продолжения."
                                )
                            if not chunk:
                                continue
                            output.write(chunk)
                            checksum = zlib.crc32(chunk, checksum)
                            received += len(chunk)
                            if progress:
                                progress(
                                    "download", completed_bytes + received,
                                    total_bytes, relative.as_posix()
                                )

                if received != expected_size or part.stat().st_size != expected_size:
                    raise ConnectionError(
                        f"Файл {relative.as_posix()} скачан не полностью "
                        f"({part.stat().st_size}/{expected_size} байт)."
                    )
                if (checksum & 0xFFFFFFFF) != expected_crc:
                    part.unlink(missing_ok=True)
                    raise RuntimeError(f"Контрольная сумма не совпала: {relative.as_posix()}")
                part.replace(target)
                completed_bytes += expected_size
                if progress:
                    progress("download", completed_bytes, total_bytes, relative.as_posix())
                last_error = None
                break
            except InterruptedError:
                raise
            except Exception as exc:
                last_error = exc
                if part.is_file() and part.stat().st_size >= expected_size:
                    part.unlink(missing_ok=True)
                if attempt < 3:
                    time.sleep(min(attempt + 1, 2))

        if last_error is not None:
            raise RuntimeError(
                f"Не удалось скачать файл клиента {relative.as_posix()} после 4 попыток: {last_error}"
            ) from last_error

    return install_root, len(files)

def rounded_polygon(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
                    radius: float, **kwargs):
    radius = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    points = [
        x1 + radius, y1, x2 - radius, y1,
        x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2,
        x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius,
        x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=18, **kwargs)


class LedgerLauncher(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1120x720")
        self.minsize(960, 640)
        self.configure(bg=BG)
        try:
            self.state("zoomed")
        except tk.TclError:
            pass

        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.busy = False
        self.phase = "idle"
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "LEDERG CRMP"
        base.mkdir(parents=True, exist_ok=True)
        self.install_location_file = base / "install_location.txt"
        self.server_cache_file = base / "server_address.txt"
        self.nickname_file = base / "nickname.txt"
        try:
            saved_nickname = self.nickname_file.read_text(encoding="utf-8")
        except OSError:
            saved_nickname = "LEDERG"
        self.nickname_var = tk.StringVar(value=sanitize_nickname(saved_nickname))
        self.nickname_entry = tk.Entry(
            self,
            textvariable=self.nickname_var,
            bg="#080808",
            fg=WHITE,
            insertbackground=RED,
            selectbackground=RED,
            selectforeground=WHITE,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground="#242424",
            highlightcolor=RED,
            font=("Segoe UI", 13, "bold"),
            takefocus=True,
        )
        self.nickname_entry.bind("<FocusOut>", self._save_nickname)
        self.nickname_entry.bind("<Return>", self._save_nickname)
        self.logo_image = None
        try:
            resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
            logo_path = resource_root / "LEDERG-CRMP-Avatar.png"
            if logo_path.is_file():
                source_logo = tk.PhotoImage(file=str(logo_path))
                factor = max(1, (source_logo.width() + 51) // 52)
                self.logo_image = source_logo.subsample(factor, factor)
        except (tk.TclError, OSError):
            self.logo_image = None
        self.server_host, self.server_port = SERVER_HOST, SERVER_PORT
        try:
            cached_endpoint = self.server_cache_file.read_text(encoding="utf-8").strip()
            if cached_endpoint:
                self.server_host, self.server_port = parse_server_address(cached_endpoint)
        except (OSError, ValueError):
            pass
        self.game_process: subprocess.Popen | None = None
        self.game_pid: int | None = None
        self.launcher_pid: int | None = None
        self.launch_monitor_attempts = 0
        self.game_poll_id = None
        self._radio_sync_running = False
        self._pending_play_after_radio = False
        self.launch_mode = DEFAULT_LAUNCH_MODE
        self.destination: Path | None = None
        try:
            remembered = self.install_location_file.read_text(encoding="utf-8").strip()
            candidate = Path(remembered) if remembered else None
            if candidate and candidate.is_dir():
                self.destination = candidate
        except OSError:
            pass
        self.client: Path | None = find_client(self.destination) if self.destination else None
        self.release_version = "unknown"
        self.update_available = False
        self.update_fingerprint = ""
        self._update_check_running = False
        self._update_prompt_open = False
        self.update_preferences_file = base / "update_preferences.json"
        try:
            update_preferences = json.loads(self.update_preferences_file.read_text(encoding="utf-8"))
            self.ignored_update_fingerprint = str(update_preferences.get("ignored_fingerprint", ""))
        except (OSError, ValueError, TypeError):
            self.ignored_update_fingerprint = ""
        self.status_text = (
            "КЛИЕНТ НАЙДЕН — ПРОВЕРЯЮ ОБНОВЛЕНИЯ"
            if self.client else "ВЫБЕРИ ПАПКУ И УСТАНОВИ LEDERG"
        )
        self.progress = 100.0 if self.client else 0.0
        self.progress_label = "ГОТОВО" if self.client else "ОЖИДАНИЕ"
        self.current_file = self.client.name if self.client else ""
        self.hover_tag = ""

        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.tag_bind("choose", "<Button-1>", self.choose_destination)
        self.canvas.tag_bind("primary", "<Button-1>", self.primary_action)
        self.canvas.tag_bind("cancel", "<Button-1>", self.cancel)
        self.canvas.tag_bind("reinstall", "<Button-1>", self.reinstall)
        self.canvas.tag_bind("button", "<Enter>", lambda _e: self.canvas.configure(cursor="hand2"))
        self.canvas.tag_bind("button", "<Leave>", lambda _e: self.canvas.configure(cursor=""))
        self.after(100, self.draw)
        self.after(100, self._poll_events)
        self.after(1800, self._start_update_check)
        self.after(60_000, self._periodic_update_check)

    def _periodic_update_check(self) -> None:
        self._start_update_check()
        self.after(60_000, self._periodic_update_check)

    def _start_update_check(self) -> None:
        if self._update_check_running or self.busy:
            return
        client = self.client if self.client and self.client.is_file() else (
            find_client(self.destination) if self.destination else None
        )
        if not client:
            return
        root = game_root_for(client)
        ignored = self.ignored_update_fingerprint
        self._update_check_running = True
        threading.Thread(
            target=self._update_check_worker,
            args=(root, ignored),
            daemon=True,
            name="LEDERG-update-check",
        ).start()

    def _update_check_worker(self, install_root: Path, ignored_fingerprint: str) -> None:
        try:
            try:
                release = fetch_release_manifest()
                manifest_url = release.get("files_manifest_url") or CLIENT_FILES_MANIFEST_URL
            except Exception:
                manifest_url = CLIENT_FILES_MANIFEST_URL
            manifest = fetch_files_manifest(manifest_url, http=HTTP)
            fingerprint = manifest["fingerprint"]
            if fingerprint == ignored_fingerprint:
                self.events.put(("update_check_done", {
                    "fingerprint": fingerprint,
                    "version": manifest["version"],
                    "available": True,
                    "ignored": True,
                }))
                return
            report = inspect_installation(install_root, manifest)
            self.events.put(("update_check_done", {
                "fingerprint": fingerprint,
                "version": manifest["version"],
                "available": report["update_available"],
                "missing": len(report["missing"]),
                "changed": len(report["changed"]),
                "removed": len(report["removed"]),
                "ignored": False,
            }))
        except Exception as exc:
            self.events.put(("update_check_done", {"error": str(exc)}))

    def _save_ignored_update_fingerprint(self, fingerprint: str) -> None:
        self.ignored_update_fingerprint = fingerprint
        try:
            self.update_preferences_file.write_text(
                json.dumps({"ignored_fingerprint": fingerprint}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def _save_nickname(self, _event=None) -> str:
        nickname = sanitize_nickname(self.nickname_var.get())
        self.nickname_var.set(nickname)
        try:
            self.nickname_file.write_text(nickname, encoding="utf-8")
        except OSError:
            pass
        return nickname

    def _on_resize(self, _event=None) -> None:
        self.draw()

    def _on_mouse_move(self, event) -> None:
        item = self.canvas.find_withtag("current")
        tags = set(self.canvas.gettags(item[0])) if item else set()
        self.hover_tag = next((t for t in ("choose", "primary", "cancel", "reinstall") if t in tags), "")
        # Canvas has no heavyweight widgets; redraw only when the active button changes.
        if getattr(self, "_last_hover", None) != self.hover_tag:
            self._last_hover = self.hover_tag
            self.draw()

    def _text(self, x, y, value, size, color, bold=False, anchor="nw", font="Segoe UI", **kwargs):
        return self.canvas.create_text(
            x, y, text=value, fill=color, anchor=anchor,
            font=(font, size, "bold" if bold else "normal"), **kwargs
        )

    def _pill_button(self, x, y, w, h, label, tag, primary=False, disabled=False):
        hot = self.hover_tag == tag
        fill = TRACK if disabled else (
            RED_HOT if primary and hot else RED if primary else "#15171d" if hot else BG_SOFT
        )
        outline = "" if primary else RED if hot else "#323641"
        rounded_polygon(
            self.canvas, x, y, x + w, y + h, h / 2,
            fill=fill, outline=outline, width=1, tags=("button", tag)
        )
        color = "#ffffff" if primary or not disabled else MUTED2
        self._text(x + w / 2, y + h / 2, label, 10 if not primary else 11,
                   color, True, anchor="center", tags=("button", tag))

    @staticmethod
    def _short_path(path: Path | None, width: int = 48) -> str:
        if not path:
            return "ПАПКА НЕ ВЫБРАНА"
        value = str(path)
        if len(value) <= width:
            return value
        return "…" + value[-(width - 1):]

    def draw(self) -> None:
        if not hasattr(self, "canvas"):
            return
        c = self.canvas
        w = max(c.winfo_width(), 960)
        h = max(c.winfo_height(), 640)
        c.delete("all")
        c.configure(bg="#000000")
        c.create_rectangle(0, 0, w, h, fill="#000000", outline="")

        # Strict black surface, restrained red signals, no cards or stock widgets.
        c.create_rectangle(0, 0, w, 3, fill=RED, outline="")
        for i in range(5):
            c.create_line(
                w - 198 + i * 25, 0, w - 158 + i * 25, 3,
                fill=RED_DARK if i % 2 == 0 else "#090909", width=2,
            )
        c.create_line(52, 91, w - 52, 91, fill="#1d1d1d", width=1)
        c.create_line(52, 91, 164, 91, fill=RED, width=2)

        # Brand header.
        if self.logo_image is not None:
            c.create_image(52, 23, image=self.logo_image, anchor="nw")
        else:
            c.create_polygon(
                55, 25, 73, 25, 63, 43, 79, 43, 53, 70, 60, 49, 47, 49,
                fill=RED, outline="",
            )
        self._text(118, 26, "LEDERG", 20, WHITE, True)
        self._text(205, 26, "CR:MP", 20, RED, True)
        self._text(119, 57, "DIRECT CLIENT  /  0.3e", 8, MUTED, True)

        dot_x = w - 281
        c.create_oval(dot_x, 36, dot_x + 7, 43, fill=RED, outline="")
        self._text(dot_x + 16, 30, "DIRECT ACCESS", 8, MUTED, True)
        self._text(w - 52, 52, f"{self.server_host}:{self.server_port}",
                   10, WHITE, True, anchor="ne", font="Consolas")
        self._text(w - 52, 69, "UDP  /  LEDERG SERVER", 7, MUTED2, True, anchor="ne")

        # Left: identity and endpoint.
        left_x = 56
        self._text(left_x, 130, "01   /   ИГРОК", 8, RED, True)
        self._text(left_x - 3, 155, "ВХОД В LEDERG", 27, WHITE, True)
        self._text(left_x, 199, "Укажи ник — лаунчер запомнит его.", 10, MUTED)

        self._text(left_x, 229, "НИКНЕЙМ", 8, MUTED, True)
        nick_width = min(360, max(260, int(w * 0.36)))
        self.nickname_entry.place_configure(
            x=left_x, y=251, width=nick_width, height=34
        )
        self.nickname_entry.lift()
        c.create_line(left_x, 291, left_x + nick_width, 291, fill="#2a2a2a", width=1)
        c.create_line(left_x, 291, left_x + min(70, nick_width), 291, fill=RED, width=2)
        self._text(left_x, 316, "СЕРВЕР", 8, MUTED, True)
        self._text(left_x, 341, "LEDERG PROJECT", 15, WHITE, True)
        c.create_oval(left_x, 379, left_x + 8, 387, fill=RED, outline="")
        self._text(left_x + 18, 374, f"{self.server_host}:{self.server_port}",
                   11, WHITE, True, font="Consolas")
        self._text(left_x, 407, "ПРЯМОЕ UDP-ПОДКЛЮЧЕНИЕ  /  БЕЗ СПИСКА СЕРВЕРОВ",
                   7, MUTED2, True)

        accent_y = h - 130
        c.create_line(left_x, accent_y, left_x + 52, accent_y, fill=RED, width=3)
        self._text(left_x, accent_y + 20, "АВТООБНОВЛЕНИЕ LEDERG", 8, WHITE, True)
        self._text(left_x, accent_y + 40, "Проверка изменений и докачка файлов", 8, MUTED)

        # Right: a single clear installation/launch flow.
        rx = int(w * 0.55)
        rw = w - rx - 56
        c.create_line(rx - 34, 127, rx - 34, h - 78, fill="#1b1b1b", width=1)
        c.create_line(rx - 34, 127, rx - 34, 181, fill=RED, width=2)
        self._text(rx, 130, "02   /   КЛИЕНТ", 8, RED, True)
        self._text(rx, 155, "ИГРОВОЙ КЛИЕНТ", 20, WHITE, True)
        self._text(rx, 199, "МЕСТО УСТАНОВКИ", 8, MUTED, True)
        self._text(
            rx, 219, self._short_path(self.destination, max(30, int(rw / 8))),
            9, WHITE if self.destination else MUTED2, True, font="Consolas",
        )
        self._pill_button(rx, 246, min(320, rw), 40, "↗   ВЫБРАТЬ ПАПКУ", "choose", False)

        self._text(rx, 307, "ЗАПУСК", 8, MUTED, True)
        if self.busy:
            button_text, button_tag = "ОСТАНОВИТЬ ЗАГРУЗКУ", "cancel"
        elif self.update_available:
            button_text, button_tag = "↻   ОБНОВИТЬ LEDERG", "primary"
        elif self.client and self.client.is_file():
            button_text, button_tag = "▶   ЗАПУСТИТЬ LEDERG", "primary"
        else:
            button_text, button_tag = "↓   СКАЧАТЬ LEDERG", "primary"
        self._pill_button(rx, 330, min(370, rw), 58, button_text, button_tag, True)

        if self.client and self.client.is_file() and not self.busy:
            self._pill_button(rx, 401, min(250, rw), 34, "ПЕРЕУСТАНОВИТЬ КЛИЕНТ", "reinstall", False)
        else:
            self._text(rx, 406, "FILES  /  RESUME  /  CRC32 VERIFY", 7, MUTED2, True)

        base_y = max(480, h - 190)
        c.create_line(rx, base_y - 22, w - 56, base_y - 22, fill="#1d1d1d", width=1)
        dot_color = RED if self.busy or self.phase == "error" else RED
        c.create_oval(rx, base_y, rx + 7, base_y + 7, fill=dot_color, outline="")
        status_limit = max(22, int((rw - 20) / 7))
        self._text(rx + 17, base_y - 5, self.status_text[:status_limit],
                   8, WHITE if self.busy else MUTED, True)
        self._text(rx, base_y + 25, self.progress_label, 7, MUTED2, True)
        self._text(w - 56, base_y + 25, f"{self.progress:.1f}%",
                   9, WHITE, True, anchor="ne", font="Consolas")
        bar_y = base_y + 46
        bar_w = rw
        rounded_polygon(c, rx, bar_y, rx + bar_w, bar_y + 6, 3, fill=TRACK, outline="")
        if self.progress > 0:
            fill_w = max(6, bar_w * min(100.0, self.progress) / 100.0)
            rounded_polygon(c, rx, bar_y, rx + fill_w, bar_y + 6, 3, fill=RED, outline="")

        details_y = bar_y + 20
        if self.phase == "download":
            detail = "ЗАГРУЗКА  /  " + self.current_file
        elif self.phase == "extract":
            detail = "РАСПАКОВКА  /  " + self.current_file
        elif self.client and self.client.is_file():
            detail = str(self.client)
        else:
            detail = "ВЫБЕРИ ПАПКУ  →  СКАЧАТЬ И УСТАНОВИТЬ"
        detail_limit = max(30, int(rw / 7))
        if len(detail) > detail_limit:
            detail = "…" + detail[-(detail_limit - 1):]
        self._text(rx, details_y, detail, 7, MUTED2, font="Consolas")

        c.create_line(52, h - 53, w - 52, h - 53, fill="#171717", width=1)
        self._text(52, h - 35, "LEDERG   /   CR:MP", 8, MUTED2, True)
        self._text(w - 52, h - 35, "BLACK CLIENT  ·  DIRECT CONNECT",
                   7, MUTED2, True, anchor="ne")
    def choose_destination(self, _event=None, start_install=False) -> None:
        if self.busy:
            return
        initial = str(Path.home() / "Documents")
        if self.destination and self.destination.is_dir():
            initial = str(self.destination)
        selected = filedialog.askdirectory(
            parent=self,
            title="Выбери папку для установки LEDERG CR:MP",
            initialdir=initial,
            mustexist=True,
        )
        if not selected:
            return
        self.destination = Path(selected)
        try:
            self.install_location_file.write_text(str(self.destination), encoding="utf-8")
        except OSError:
            pass
        self.client = find_client(self.destination)
        if self.client:
            self.status_text = "КЛИЕНТ НАЙДЕН — МОЖНО ЗАПУСКАТЬ"
            self.progress = 100.0
            self.progress_label = "ГОТОВО"
            self.current_file = self.client.name
        else:
            self.status_text = "ПАПКА ВЫБРАНА — ГОТОВ К УСТАНОВКЕ"
            self.progress = 0.0
            self.progress_label = "ОЖИДАНИЕ"
            self.current_file = ""
        self.draw()
        if start_install and not self.client:
            self.start_install()

    def primary_action(self, _event=None) -> None:
        if self.busy:
            return
        if self.update_available:
            self.start_install()
            return
        if self.client and self.client.is_file():
            self.play()
            return
        if not self.destination:
            self.choose_destination(start_install=True)
            return
        self.start_install()

    def reinstall(self, _event=None) -> None:
        if self.busy:
            return
        if not self.destination:
            self.choose_destination(start_install=True)
            return
        self.client = None
        self.start_install()

    def start_install(self) -> None:
        if self.busy:
            return
        game_running = (
            (self.game_pid is not None and is_process_running(self.game_pid))
            or (self.game_process is not None and self.game_process.poll() is None)
        )
        if game_running:
            messagebox.showwarning(
                APP_NAME,
                "Сначала закрой игру. LEDERG нельзя безопасно обновлять, пока игровой клиент запущен.",
                parent=self,
            )
            return
        if not self.destination:
            self.choose_destination(start_install=True)
            return

        self.destination.mkdir(parents=True, exist_ok=True)
        self.cancel_event.clear()
        self.busy = True
        self.phase = "download"
        self.progress = 0.0
        self.progress_label = "ПОДГОТОВКА ЗАГРУЗКИ"
        self.current_file = "GTA Criminal Russia/"
        self.status_text = "СОЕДИНЯЮСЬ С СЕРВЕРОМ LEDERG"
        self.draw()
        self.worker = threading.Thread(target=self._install_worker, args=(self.destination,), daemon=True)
        self.worker.start()

    def cancel(self, _event=None) -> None:
        if self.busy:
            self.status_text = "ОСТАНАВЛИВАЮ… СОХРАНЯЮ ПРОГРЕСС"
            self.cancel_event.set()
            self.draw()

    def _install_worker(self, destination: Path) -> None:
        try:
            self.events.put(("status", "Получаю настройки клиента LEDERG…"))
            try:
                release = fetch_release_manifest()
            except Exception:
                # The static CloudPub manifest remains usable when GitHub is temporarily unreachable.
                release = {
                    "version": "fallback",
                    "archive_url": ZIP_URL,
                    "files_manifest_url": CLIENT_FILES_MANIFEST_URL,
                    "archive_size": 0,
                    "sha256": "",
                    "client_executable": DEFAULT_CLIENT_EXECUTABLE,
                    "launch_mode": DEFAULT_LAUNCH_MODE,
                }

            self.events.put(("status", f"Скачиваю клиент LEDERG {release['version']}…"))
            count = 0
            archive = None
            installed_manifest = None
            if release.get("files_manifest_url"):
                installed_manifest = fetch_files_manifest(release["files_manifest_url"], http=HTTP)
                self.events.put((
                    "phase", "download", "СКАЧИВАЮ ОБНОВЛЁННЫЕ ФАЙЛЫ…",
                    "GTA Criminal Russia/"
                ))
                _install_root, count = download_client_folder(
                    destination,
                    release["files_manifest_url"],
                    progress=lambda phase, done, total, label: self.events.put(
                        ("progress", phase, done, total, label)
                    ),
                    cancel=self.cancel_event,
                    manifest_payload=installed_manifest,
                )
            else:
                archive = download_archive(
                    destination,
                    progress=lambda phase, done, total, label: self.events.put(
                        ("progress", phase, done, total, label)
                    ),
                    cancel=self.cancel_event,
                    url=release["archive_url"],
                    expected_sha256=release.get("sha256", ""),
                    expected_size=int(release.get("archive_size", 0) or 0),
                )
                if self.cancel_event.is_set():
                    raise InterruptedError("Установка остановлена. Архив сохранён.")
                self.events.put(("phase", "extract", "ПРОВЕРЯЮ АРХИВ…", "GTA Criminal Russia.zip"))
                count = safe_extract_zip(
                    archive,
                    destination,
                    progress=lambda done, total, name: self.events.put(
                        ("progress", "extract", done, total, name)
                    ),
                    cancel=self.cancel_event,
                )

            if self.cancel_event.is_set():
                raise InterruptedError("Загрузка остановлена. Уже скачанные файлы сохранены.")
            self.events.put(("status", "ПРОВЕРЯЮ ИГРОВОЙ КЛИЕНТ…"))
            client = find_client(destination, release.get("client_executable"))
            if not client:
                raise RuntimeError(
                    "Файлы скачаны, но игровой клиент не найден.\n\n"
                    "Проверь, что папка содержит multiplayer_c/multiplayer_browser_cr.exe "
                    "либо совместимый CR:MP-клиент."
                )
            if installed_manifest is not None:
                apply_successful_update(game_root_for(client), installed_manifest)
            if archive is not None:
                archive.unlink(missing_ok=True)
            final_version = installed_manifest["version"] if installed_manifest else release.get("version", "unknown")
            self.events.put(("installed", str(client), count, final_version, release.get("launch_mode", DEFAULT_LAUNCH_MODE)))
        except InterruptedError as exc:
            self.events.put(("cancelled", str(exc)))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "radio_sync_done":
                    self._radio_sync_running = False
                    should_play = self._pending_play_after_radio
                    self._pending_play_after_radio = False
                    result = event[1]
                    if result.get("error"):
                        self.status_text = "РАДИО НЕДОСТУПНО — ЗАПУСКАЮ LEDERG"
                    else:
                        radio_result = result.get("result", {})
                        if radio_result.get("ok"):
                            station_count = len(radio_result.get("stations", []))
                            track_count = int(radio_result.get("tracks", 0))
                            self.status_text = f"РАДИО LEDERG: {station_count} СТАНЦИЙ / {track_count} ТРЕКОВ"
                        else:
                            self.status_text = "РАДИО НЕ ОБНОВЛЕНО — ИСПОЛЬЗУЮ ТЕКУЩИЕ ТРЕКИ"
                    self.draw()
                    if should_play:
                        self.after(100, lambda: self.play(_radio_synced=True))
                    continue
                elif kind == "update_check_done":
                    self._update_check_running = False
                    result = event[1]
                    if result.get("error"):
                        if self.client and not self.busy:
                            self.status_text = "КЛИЕНТ НАЙДЕН — ПРОВЕРКА ОБНОВЛЕНИЙ НЕДОСТУПНА"
                    else:
                        fingerprint = str(result.get("fingerprint", ""))
                        version = str(result.get("version", "unknown"))
                        self.update_fingerprint = fingerprint
                        self.release_version = version
                        if result.get("available"):
                            self.update_available = True
                            if result.get("ignored"):
                                self.status_text = "ОБНОВЛЕНИЕ ДОСТУПНО — НАЖМИ «ОБНОВИТЬ LEDERG»"
                            else:
                                game_running = (
                                    (self.game_pid is not None and is_process_running(self.game_pid))
                                    or (self.game_process is not None and self.game_process.poll() is None)
                                )
                                if game_running:
                                    self.status_text = "ОБНОВЛЕНИЕ ДОСТУПНО — ЗАКРОЙ ИГРУ"
                                elif not self._update_prompt_open:
                                    self._update_prompt_open = True
                                    self.status_text = f"ДОСТУПНО ОБНОВЛЕНИЕ LEDERG {version}"
                                    self.draw()
                                    changes = []
                                    if result.get("missing"):
                                        changes.append(f"Новых или отсутствующих файлов: {result['missing']}")
                                    if result.get("changed"):
                                        changes.append(f"Изменённых файлов: {result['changed']}")
                                    if result.get("removed"):
                                        changes.append(f"Устаревших файлов к удалению: {result['removed']}")
                                    detail = "\n".join(changes) or "Состав клиента изменился."
                                    accepted = messagebox.askyesno(
                                        "Обновление LEDERG",
                                        f"Доступно обновление клиента LEDERG CR:MP.\n\n"
                                        f"Версия: {version}\n{detail}\n\n"
                                        "Скачать изменённые файлы и обновить клиент сейчас?",
                                        parent=self,
                                    )
                                    self._update_prompt_open = False
                                    if accepted:
                                        self.start_install()
                                    else:
                                        self._save_ignored_update_fingerprint(fingerprint)
                                        self.status_text = "ОБНОВЛЕНИЕ ДОСТУПНО — НАЖМИ «ОБНОВИТЬ LEDERG»"
                        else:
                            self.update_available = False
                            if self.client and not self.busy:
                                self.status_text = f"КЛИЕНТ LEDERG АКТУАЛЕН · {version}"
                elif kind == "status":
                    self.status_text = event[1]
                elif kind == "phase":
                    self.phase, self.status_text, self.current_file = event[1:]
                    self.progress = 0.0
                    self.progress_label = "РАСПАКОВКА"
                elif kind == "progress":
                    _, phase, done, total, label = event
                    self.phase = phase
                    self.current_file = label
                    self.progress = min(100.0, (done * 100.0 / total)) if total else 0.0
                    if phase == "download":
                        self.progress_label = (
                            f"СКАЧАНО {self._bytes(done)} / {self._bytes(total)}"
                            if total else f"СКАЧАНО {self._bytes(done)}"
                        )
                        self.status_text = "СКАЧИВАЮ ФАЙЛЫ LEDERG"
                    elif phase == "extract":
                        self.progress_label = (
                            f"РАСПАКОВАНО {self._bytes(done)} / {self._bytes(total)}"
                            if total else f"РАСПАКОВАНО {self._bytes(done)}"
                        )
                        self.status_text = "РАСПАКОВЫВАЮ ИГРОВЫЕ ФАЙЛЫ"
                    else:
                        self.status_text = label
                elif kind == "installed":
                    self.client = Path(event[1])
                    self.release_version = event[3] if len(event) > 3 else "unknown"
                    self.launch_mode = event[4] if len(event) > 4 else DEFAULT_LAUNCH_MODE
                    self.update_available = False
                    self.update_fingerprint = ""
                    self._save_ignored_update_fingerprint("")
                    self.status_text = "КЛИЕНТ ОБНОВЛЁН — МОЖНО ИГРАТЬ"
                    self.progress = 100.0
                    self.progress_label = "ГОТОВО"
                    self.phase = "ready"
                    self.current_file = self.client.name
                    self.busy = False
                    self.draw()
                    messagebox.showinfo(
                        APP_NAME,
                        f"Клиент установлен.\n\nНайдено файлов: {event[2]}\n\nНажми «ЗАПУСТИТЬ LEDERG».",
                        parent=self,
                    )
                    continue
                elif kind == "cancelled":
                    self.busy = False
                    self.phase = "paused"
                    self.status_text = (
                        "ОБНОВЛЕНИЕ ПРИОСТАНОВЛЕНО — МОЖНО ПРОДОЛЖИТЬ"
                        if self.update_available else "ПРИОСТАНОВЛЕНО — ПРОГРЕСС СОХРАНЁН"
                    )
                    self.progress_label = "МОЖНО ПРОДОЛЖИТЬ"
                    self.current_file = ""
                elif kind == "error":
                    self.busy = False
                    self.phase = "error"
                    self.status_text = (
                        "ОБНОВЛЕНИЕ НЕ ЗАВЕРШЕНО — ПОВТОРИ ПОЗЖЕ"
                        if self.update_available else "УСТАНОВКА НЕ ЗАВЕРШЕНА"
                    )
                    self.current_file = ""
                    messagebox.showerror(APP_NAME, event[1], parent=self)
                self.draw()
        except queue.Empty:
            pass
        self.after(120, self._poll_events)

    @staticmethod
    def _bytes(value: int) -> str:
        if value <= 0:
            return "0 Б"
        if value >= 1024 ** 3:
            return f"{value / (1024 ** 3):.2f} ГБ"
        return f"{value / (1024 ** 2):.0f} МБ"

    def _radio_sync_worker(self, game_root: Path) -> None:
        try:
            result = sync_radio_repository(game_root)
            self.events.put(("radio_sync_done", {"result": result}))
        except Exception as exc:
            self.events.put(("radio_sync_done", {"error": str(exc)}))

    def play(self, _radio_synced: bool = False) -> None:
        client = self.client if self.client and self.client.is_file() else (
            find_client(self.destination) if self.destination else None
        )
        if not client:
            messagebox.showwarning(APP_NAME, "Сначала скачай и установи клиент.", parent=self)
            return
        if self.game_pid is not None and is_process_running(self.game_pid):
            self.status_text = "ИГРА УЖЕ ЗАПУЩЕНА"
            self.draw()
            return
        if self.game_process is not None and self.game_process.poll() is None:
            self.status_text = "ИГРА УЖЕ ЗАПУСКАЕТСЯ"
            self.draw()
            return
        if self.game_process is not None and self.game_pid is None and self.launch_monitor_attempts < 20:
            self.status_text = "ПРОВЕРЯЮ ЗАПУСК ИГРЫ..."
            self.draw()
            return
        if not _radio_synced:
            if self._radio_sync_running:
                self._pending_play_after_radio = True
                self.status_text = "СИНХРОНИЗИРУЮ РАДИО LEDERG..."
                self.draw()
                return
            self._radio_sync_running = True
            self._pending_play_after_radio = True
            self.status_text = "ПРОВЕРЯЮ РАДИОСТАНЦИИ LEDERG..."
            self.current_file = "LEDERG-radio-crmp"
            self.draw()
            threading.Thread(
                target=self._radio_sync_worker,
                args=(game_root_for(client),),
                daemon=True,
                name="LEDERG-radio-sync",
            ).start()
            return
        try:
            nickname = self._save_nickname()
            self.server_host, self.server_port = load_server_address(self.server_cache_file)
            root = game_root_for(client)
            launch_mode = getattr(self, "launch_mode", DEFAULT_LAUNCH_MODE)
            if launch_mode == "direct":
                helper = direct_launcher_path().resolve()
                if not helper.is_file():
                    raise FileNotFoundError(
                        "В сборке отсутствует LEDERG-DirectLaunch.exe. "
                        "Скачай последнюю версию лаунчера и запусти её повторно."
                    )
                command = build_direct_launcher_command(
                    helper, root, self.server_host, self.server_port, nickname
                )
                write_launcher_log(
                    f"Starting hidden direct-connect helper; helper={helper}; "
                    f"game={root / 'gta_sa.exe'}; dll={root / 'multiplayer_c' / 'san_andreas_multiplayer.dll'}; "
                    f"cwd={root}; endpoint={self.server_host}:{self.server_port}; nickname={nickname}"
                )
                self.status_text = (
                    f"ПОДКЛЮЧАЮ К {self.server_host}:{self.server_port} — БЕЗ БРАУЗЕРА"
                )
                self.draw()
                self.game_process = spawn_direct_launcher(command, root)
                write_launcher_log(
                    f"Hidden direct-connect helper started; pid={self.game_process.pid}; "
                    f"endpoint={self.server_host}:{self.server_port}"
                )
            elif launch_mode == "browser-ui":
                self.status_text = f"ОТКРЫВАЮ CR:MP — {self.server_host}:{self.server_port}"
                self.draw()
                self.game_process = connect_via_browser_ui(
                    client, self.server_host, self.server_port, nickname
                )
            else:
                command = build_launch_command(
                    client, self.server_host, self.server_port, nickname, launch_mode
                )
                self.game_process = subprocess.Popen(
                    command, cwd=str(root), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                )
            self.launcher_pid = self.game_process.pid
            self.game_pid = None
            self.launch_monitor_attempts = 0
            self.status_text = (
                f"ПОДКЛЮЧАЮСЬ БЕЗ БРАУЗЕРА — {self.server_host}:{self.server_port}"
                if launch_mode in {"direct", "samp-cli"}
                else f"КЛИЕНТ ОТКРЫТ — {self.server_host}:{self.server_port}"
            )
            self.draw()
            if self.game_poll_id is None:
                self.game_poll_id = self.after(1000, self._monitor_game)
        except Exception as exc:
            write_launcher_log(f"ERROR starting client: {exc!r}")
            messagebox.showerror(APP_NAME, f"Не удалось запустить клиент:\n{exc}", parent=self)

    def _monitor_game(self) -> None:
        self.game_poll_id = None
        process = self.game_process
        if process is None:
            return

        if self.launch_mode in {"direct", "samp-cli"}:
            if self.game_pid is None and self.launcher_pid:
                self.game_pid = find_child_game_pid(self.launcher_pid)
                if self.game_pid is None and self.game_process and self.game_process.poll() is None:
                    self.game_pid = self.launcher_pid

            if self.game_pid is None:
                self.launch_monitor_attempts += 1
                if self.launch_monitor_attempts < 25:
                    self.status_text = "ЗАПУСКАЮ ИГРУ БЕЗ БРАУЗЕРА..."
                    self.game_poll_id = self.after(400, self._monitor_game)
                else:
                    code = process.poll()
                    write_launcher_log(
                        f"Game process was not detected after launch; launcher_pid={self.launcher_pid}; "
                        f"exit_code={code}"
                    )
                    self.status_text = (
                        "ИГРА НЕ ОБНАРУЖЕНА — СМОТРИ ЖУРНАЛ"
                        if code in (None, 0)
                        else f"КЛИЕНТ ЗАВЕРШИЛСЯ С КОДОМ {code} — СМОТРИ ЖУРНАЛ"
                    )
                    self.game_process = None
                    self.launcher_pid = None
                self.draw()
                return

            if is_process_running(self.game_pid):
                self.status_text = (
                    f"ИГРА РАБОТАЕТ — {self.server_host}:{self.server_port}"
                )
                self.game_poll_id = self.after(1500, self._monitor_game)
            else:
                self.status_text = "ИГРА ЗАКРЫТА"
                self.game_process = None
                self.game_pid = None
                self.launcher_pid = None
            self.draw()
            return

        code = process.poll()
        if code is None:
            self.status_text = (
                f"КЛИЕНТ РАБОТАЕТ — {self.server_host}:{self.server_port}"
            )
            self.game_poll_id = self.after(1500, self._monitor_game)
        else:
            self.status_text = (
                "ИГРА ЗАКРЫТА" if code == 0
                else f"КЛИЕНТ ЗАВЕРШИЛСЯ С КОДОМ {code}"
            )
            self.game_process = None
        self.draw()


if __name__ == "__main__":
    app = LedgerLauncher()
    app.mainloop()
