from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
import traceback
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from ctypes import wintypes
from tkinter import filedialog

from client_updates import apply_successful_update, fetch_files_manifest, inspect_installation
from launcher import (
    APP_NAME,
    CLIENT_FILES_MANIFEST_URL,
    DEFAULT_CLIENT_EXECUTABLE,
    DEFAULT_LAUNCH_MODE,
    HTTP,
    SERVER_HOST,
    SERVER_PORT,
    SERVER_IP_URL,
    ZIP_URL,
    CLIENT_INTEGRITY_FILENAME,
    ENB_DISABLED_BACKUP,
    build_direct_launcher_command,
    direct_launcher_path,
    spawn_direct_launcher,
    connect_via_browser_ui,
    download_archive,
    download_client_folder,
    fetch_release_manifest,
    find_client,
    find_direct_game_client,
    game_root_for,
    load_server_address,
    parse_server_address,
    safe_extract_zip,
    finalize_client_integrity_manifest,
    inspect_client_installation,
    extract_faulting_module,
    extract_faulting_module_path,
    quarantine_faulting_optional_module,
    select_client_crash_events,
    sanitize_nickname,
    write_launcher_log,
    query_samp_server,
    format_client_exit_code,
)

DATA_HOME = Path(
    os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
) / "LEDERG CRMP"
WEB_ROOT = Path(os.environ.get("LEDERG_WEB_ROOT", Path(__file__).resolve().parent / "web"))
STATE_FILE = DATA_HOME / "install_location.txt"
NICK_FILE = DATA_HOME / "nickname.txt"

SERVER_FILE = DATA_HOME / "server_address.txt"
AUTO_REPAIR_STATE_FILE = DATA_HOME / "auto-repair-state.json"
AUTO_REPAIR_COOLDOWN_SECONDS = 24 * 60 * 60
AUTO_REPAIR_FAILURE_RETRY_SECONDS = 15 * 60

_REPAIR_PHASE_LABELS = {
    "manifest": "ПОДГОТОВКА ВОССТАНОВЛЕНИЯ",
    "download": "СКАЧИВАНИЕ АРХИВА",
    "extract": "РАСПАКОВКА АРХИВА",
    "integrity": "ПРОВЕРКА ЦЕЛОСТНОСТИ",
    "schedule": "ЗАПУСК АВТОВОССТАНОВЛЕНИЯ",
    "repair": "АВТОВОССТАНОВЛЕНИЕ",
    "error": "ОШИБКА ВОССТАНОВЛЕНИЯ",
    "ready": "ПРОВЕРКА КЛИЕНТА",
}


def diagnose_auto_repair_failure(detail: str, phase: str = "") -> dict:
    """Turn the installer exception into a plain-language cause and next action."""
    raw = str(detail or "").strip()
    folded = raw.casefold()
    phase_key = str(phase or "repair").casefold()
    phase_label = _REPAIR_PHASE_LABELS.get(phase_key, "АВТОВОССТАНОВЛЕНИЕ")

    problem = ""
    advice = ""

    if (
        "недостаточно свободного места" in folded
        or "not enough free space" in folded
        or "no space left on device" in folded
        or "winerror 112" in folded
        or "errno 28" in folded
    ):
        problem = "На диске не хватает свободного места"
        advice = (
            "Освободи место на диске установки. Для архива около 4 ГБ требуется дополнительное "
            "пространство для распаковки. В ошибке ниже указано, сколько места нужно и сколько доступно."
        )
    elif (
        ("403" in folded or "forbidden" in folded)
        and ("google drive" in folded or "drive" in folded or "http" in folded)
    ):
        problem = "Google Drive отказал в доступе к архиву (HTTP 403)"
        advice = (
            "Проверь, что исходный файл GTA Criminal Russia.zip существует и доступен всем, "
            "у кого есть ссылка. Если доступ уже открыт, Google мог временно ограничить скачивание; "
            "повтори попытку позже."
        )
    elif (
        "404" in folded
        and ("google drive" in folded or "drive" in folded or "http" in folded or "not found" in folded)
    ):
        problem = "Google Drive не нашёл архив (HTTP 404)"
        advice = (
            "Проверь, что архив не удалён, не перемещён и что ссылка на нужный файл всё ещё актуальна."
        )
    elif (
        re.search(r"\b5\d{2}\b", folded)
        and ("http" in folded or "google drive" in folded or "drive" in folded)
    ):
        problem = "Google Drive вернул временную серверную ошибку"
        advice = (
            "Это HTTP-ошибка на стороне сервиса или промежуточного шлюза. "
            "Повтори загрузку позже; если ошибка повторяется, проверь доступ к архиву и сетевой фильтр."
        )
    elif (
        "загрузка оборвалась раньше времени" in folded
        or "скачан не полностью" in folded
        or "incomplete read" in folded
        or "incompleteread" in folded
        or "получено " in folded and " байт" in folded and " из " in folded
    ):
        problem = "Загрузка архива завершилась не полностью"
        advice = (
            "Сервер или соединение оборвали передачу до получения всех байтов. "
            "Лаунчер должен продолжить частичную загрузку; повтори попытку и проверь стабильность соединения."
        )
    elif (
        "429" in folded
        or "quotaexceeded" in folded
        or "too many requests" in folded
        or "download quota" in folded
        or "лимит скачивания" in folded
    ):
        problem = "Google Drive ограничил частоту или объём скачивания"
        advice = (
            "Это ограничение со стороны хранилища, а не повреждение игрового клиента. "
            "Подожди и повтори попытку; если повторяется, проверь лимиты доступа к архиву."
        )
    elif (
        "timed out" in folded
        or "timeout" in folded
        or "время ожидания" in folded
        or "connection reset" in folded
        or "connection aborted" in folded
        or "remote end closed" in folded
        or "failed to establish a new connection" in folded
    ):
        problem = "Соединение с Google Drive прервалось или не ответило вовремя"
        advice = (
            "Проверь интернет и сетевой фильтр/VPN. Частичный архив сохраняется для продолжения; "
            "повтори загрузку, не удаляя его вручную."
        )
    elif (
        "вернул html" in folded
        or "html-страницу вместо zip" in folded
        or "ответ сервера не похож на zip" in folded
        or "not a zip archive" in folded
        or "file is not a zip" in folded
    ):
        problem = "Вместо ZIP-архива пришла веб-страница"
        advice = (
            "Google Drive не выдал сам архив — обычно это страница доступа, подтверждения или ограничения. "
            "Проверь публичный доступ к файлу и повтори попытку."
        )
    elif (
        "badzipfile" in folded
        or "не является целым zip-архивом" in folded
        or "crc" in folded and ("zip" in folded or "архив" in folded)
        or "zipfile.badzipfile" in folded
    ):
        problem = "Архив повреждён или скачан не полностью"
        advice = (
            "Повтори загрузку и проверку. Если ошибка повторяется на том же месте, исходный ZIP на Google Drive "
            "может быть повреждён — потребуется проверить или перезалить архив."
        )
    elif (
        "сначала полностью закрой cr:mp" in folded
        or "игровой клиент ещё запущен" in folded
        or "игровой клиент еще запущен" in folded
        or "установка уже выполняется" in folded
    ):
        problem = "CR:MP всё ещё запущен или другая операция установки не завершена"
        advice = (
            "Полностью закрой игру и дождись окончания другой установки/проверки. "
            "Затем нажми «Восстановить» ещё раз; файлы не будут перезаписываться на работающей игре."
        )
    elif (
        "permissionerror" in folded
        or "access is denied" in folded
        or "доступ запрещён" in folded
        or "winerror 5" in folded
        or "permission denied" in folded
    ):
        problem = "Windows не разрешила записать файлы в выбранную папку"
        advice = (
            "Полностью закрой GTA/CR:MP и проверь права на папку установки. При необходимости выбери папку, "
            "в которую текущий пользователь Windows может записывать файлы."
        )
    elif "sha-256" in folded or "sha256" in folded or "контрольная сумма" in folded:
        problem = "Контрольная сумма файла не совпала"
        advice = (
            "Файл отличается от ожидаемого эталона. Повтори загрузку; если несоответствие повторится, "
            "нужно проверить исходный архив или манифест, а не продолжать запускать повреждённый клиент."
        )
    elif (
        "grand_theft_auto_san_andreas.dll" in folded
        or "multiplayer_browser_cr.exe" in folded
        or "игровой runtime" in folded
        or "runtime не найден" in folded
    ):
        problem = "В распакованном клиенте отсутствует обязательный компонент CR:MP"
        advice = (
            "Архив не дал полный набор файлов или распаковка завершилась не полностью. "
            "Повтори восстановление; если ошибка останется, проверь состав исходного ZIP."
        )
    elif (
        "unsafe path" in folded
        or "небезопасный путь" in folded
        or "выходит за пределы" in folded
        or "symbolic link" in folded
    ):
        problem = "Проверка безопасности остановила распаковку архива"
        advice = (
            "Лаунчер обнаружил путь файла, который выходит за границы папки игры или небезопасную ссылку. "
            "Архив не был распакован полностью; проверь исходный ZIP."
        )
    else:
        problem = f"Не удалось завершить этап «{phase_label.lower()}»"
        advice = (
            "Ниже показана исходная ошибка. Она сохранена без подмены общим сообщением; "
            "по этой строке можно установить точную причину, а кнопка «ЖУРНАЛ» содержит полный traceback."
        )

    return {
        "problem": problem,
        "advice": advice,
        "detail": raw[:2400] or "Лаунчер не получил текст исключения; открой «ЖУРНАЛ» для полной диагностики.",
        "phase": phase_key,
        "phase_label": phase_label,
    }


def parse_windows_crash_event_payload(
    payload_text: str, executable_names: list[str] | tuple[str, ...]
) -> list[str]:
    """Return Windows Application Error events that mention one of our game binaries."""
    try:
        payload = json.loads(payload_text)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(payload, dict):
        events = [payload]
    elif isinstance(payload, list):
        events = payload
    else:
        return []

    names = {
        str(name).replace("\\", "/").rsplit("/", 1)[-1].casefold()
        for name in executable_names
        if str(name).strip()
    }
    if not names:
        return []

    matches = []
    for event in events:
        if not isinstance(event, dict):
            continue
        message = str(event.get("Message") or "")
        folded_message = message.casefold()
        if not message or not any(name in folded_message for name in names):
            continue
        timestamp = str(event.get("TimeCreated") or "unknown time")
        event_id = str(event.get("Id") if event.get("Id") is not None else "?")
        provider = str(event.get("ProviderName") or "unknown provider")
        matches.append(
            f"Windows Application event: time={timestamp}; id={event_id}; "
            f"provider={provider}\n{message}"
        )
    return matches[:10]


def collect_windows_crash_events(
    executable_names: list[str] | tuple[str, ...],
    *,
    started_at: float | None = None,
    runner=None,
    sleeper=None,
    attempts: int = 3,
) -> list[str]:
    """Query recent Application Error/WER records after a game crash, without elevation."""
    if os.name != "nt":
        return []
    normalized_names = [
        str(name).replace("\\", "/").rsplit("/", 1)[-1]
        for name in executable_names
        if str(name).strip()
    ]
    if not normalized_names:
        return []

    runner = runner or subprocess.run
    sleeper = sleeper or time.sleep
    launch_epoch = float(started_at if started_at is not None else time.time() - 600.0)
    from_epoch_ms = max(0, int((launch_epoch - 3.0) * 1000))
    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        f"$from=[DateTimeOffset]::FromUnixTimeMilliseconds({from_epoch_ms}).LocalDateTime; "
        "$events = @(Get-WinEvent -FilterHashtable "
        "@{LogName='Application'; Id=1000,1001; StartTime=$from} "
        "-MaxEvents 50 -ErrorAction SilentlyContinue | "
        "Select-Object @{Name='TimeCreated';Expression={$_.TimeCreated.ToString('o')}}, "
        "Id, ProviderName, Message); "
        "if ($events.Count -gt 0) { $events | ConvertTo-Json -Depth 3 -Compress }"
    )
    command = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    ]
    for attempt in range(max(1, int(attempts))):
        try:
            result = runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                matches = parse_windows_crash_event_payload(
                    result.stdout or "", normalized_names
                )
                if matches:
                    return matches
            elif getattr(result, "stderr", ""):
                write_launcher_log(
                    "Windows crash-event query returned an error: "
                    + str(result.stderr).strip()[:1200]
                )
        except Exception as exc:
            write_launcher_log(f"Could not query Windows Application crash events: {exc!r}")
        if attempt + 1 < max(1, int(attempts)):
            sleeper(0.9)
    return []

VK_X = 0x58
VK_T = 0x54
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
X_PANEL_COMMAND = "/xpanel"


def should_trigger_xpanel_hotkey(
    *, key_down: bool, was_down: bool, foreground_pid: int, game_pid: int | None
) -> bool:
    """Trigger only on a fresh X press while the exact launched game is foreground."""
    return bool(
        key_down
        and not was_down
        and game_pid
        and foreground_pid
        and foreground_pid == game_pid
    )


def _foreground_process_id() -> tuple[int, int]:
    """Return (foreground HWND, owning PID), safely returning zero on failure."""
    try:
        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return 0, 0
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return hwnd, int(pid.value)
    except Exception:
        return 0, 0


def is_game_window_process(image_path: str, game_root: Path) -> bool:
    """Accept only the two expected game executables inside the selected client root."""
    if not image_path:
        return False
    try:
        root = os.path.normcase(os.path.abspath(os.fspath(game_root)))
        image = os.path.normcase(os.path.abspath(image_path))
        if os.path.commonpath((root, image)) != root:
            return False
        return os.path.basename(image).lower() in {
            "gta_sa.exe",
            "grand_theft_auto_san_andreas.dll",
        }
    except (OSError, ValueError):
        return False


def _process_image_path(process_id: int) -> str:
    """Read an image path without importing psutil or querying unrelated processes."""
    if not process_id:
        return ""
    kernel32 = ctypes.windll.kernel32
    process = kernel32.OpenProcess(0x1000, False, int(process_id))  # QUERY_LIMITED_INFORMATION
    if not process:
        return ""
    try:
        capacity = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if not kernel32.QueryFullProcessImageNameW(
            process, 0, buffer, ctypes.byref(capacity)
        ):
            return ""
        return buffer.value
    except Exception:
        return ""
    finally:
        kernel32.CloseHandle(process)


def _foreground_game_process_id(game_root: Path, launched_pid: int) -> int:
    """Return the game PID only for the launcher process or its known client executable."""
    _, foreground_pid = _foreground_process_id()
    if not foreground_pid:
        return 0
    if foreground_pid == launched_pid:
        return foreground_pid
    image_path = _process_image_path(foreground_pid)
    return foreground_pid if is_game_window_process(image_path, game_root) else 0


def _send_vk(user32, virtual_key: int, *, key_up: bool = False) -> None:
    flags = KEYEVENTF_KEYUP if key_up else 0
    user32.keybd_event(virtual_key, 0, flags, 0)


def _send_layout_character(user32, character: str, keyboard_layout) -> bool:
    """Type one ASCII command character using the game's active keyboard layout."""
    mapped = int(user32.VkKeyScanExW(character, keyboard_layout))
    if mapped == -1:
        return False
    virtual_key = mapped & 0xFF
    modifiers = (mapped >> 8) & 0xFF
    modifier_keys = []
    if modifiers & 1:
        modifier_keys.append(VK_SHIFT)
    if modifiers & 2:
        modifier_keys.append(VK_CONTROL)
    if modifiers & 4:
        modifier_keys.append(VK_MENU)
    for modifier in modifier_keys:
        _send_vk(user32, modifier)
    _send_vk(user32, virtual_key)
    _send_vk(user32, virtual_key, key_up=True)
    for modifier in reversed(modifier_keys):
        _send_vk(user32, modifier, key_up=True)
    return True


def _type_xpanel_command(game_pid: int) -> bool:
    """Open SA:MP chat and send /xpanel only while our game remains foreground."""
    hwnd, foreground_pid = _foreground_process_id()
    if not hwnd or foreground_pid != game_pid:
        return False
    user32 = ctypes.windll.user32
    thread_id = int(user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None) or 0)
    keyboard_layout = user32.GetKeyboardLayout(thread_id)
    # T is a game action key. Allow the game a moment to open its chat input.
    _send_vk(user32, VK_T)
    _send_vk(user32, VK_T, key_up=True)
    time.sleep(0.14)
    if _foreground_process_id()[1] != game_pid:
        return False
    for character in X_PANEL_COMMAND:
        if not _send_layout_character(user32, character, keyboard_layout):
            write_launcher_log(
                f"X hotkey could not map character {character!r} under the game keyboard layout."
            )
            return False
        time.sleep(0.018)
    _send_vk(user32, VK_RETURN)
    _send_vk(user32, VK_RETURN, key_up=True)
    return True


class LauncherApi:
    """Narrow JavaScript-to-native bridge. All game/file operations stay native."""

    def __init__(self) -> None:
        DATA_HOME.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._busy = False
        self._checking_client = False
        self._phase = "idle"
        self._status = "Проверяю папку клиента…"
        self._progress = 0.0
        self._progress_label = "ПРОВЕРКА"
        self._current_file = ""
        self._error = ""
        self._repair_failure: dict = self._load_saved_repair_failure()
        self._process: subprocess.Popen | None = None
        self._xpanel_stop: threading.Event | None = None
        self._game_root: Path | None = None
        self._endpoint = f"{SERVER_HOST}:{SERVER_PORT}"
        self._server_online: bool | None = None
        self._server_ping_ms: int | None = None
        self._server_method = "none"
        self._server_detail = "Проверяю доступность сервера…"
        self._server_checked_at = ""
        self._last_exit_code: int | None = None
        self._client_health: dict = {
            "healthy": False,
            "root": "",
            "issues": [],
            "warnings": [],
            "summary": "Автопроверка клиента запускается…",
            "integrity_mode": "pending",
        }
        self._nickname = self._read_text(NICK_FILE, "LEDERG")
        self._install_path = self._read_install_path()
        self._refresh_install_root()
        threading.Thread(target=self._server_monitor, daemon=True, name="LEDERG-server-monitor").start()
        threading.Thread(target=self._startup_check, daemon=True, name="LEDERG-client-check").start()

    @staticmethod
    def _load_saved_repair_failure() -> dict:
        """Restore the last failed auto-repair diagnosis so a restart does not hide it."""
        try:
            payload = json.loads(AUTO_REPAIR_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(payload, dict) or str(payload.get("result") or "").casefold() != "failed":
            return {}
        detail = str(payload.get("detail") or payload.get("reason") or "")
        phase = str(payload.get("phase") or "repair")
        diagnosis = diagnose_auto_repair_failure(detail, phase)
        try:
            attempted_at = float(payload.get("attempted_at") or 0)
        except (ValueError, TypeError):
            attempted_at = 0.0
        remaining = max(
            0.0,
            AUTO_REPAIR_FAILURE_RETRY_SECONDS - max(0.0, time.time() - attempted_at),
        )
        diagnosis["retry_minutes"] = max(0, int((remaining + 59) // 60))
        return diagnosis

    @staticmethod
    def _read_text(path: Path, default: str) -> str:
        try:
            return path.read_text(encoding="utf-8-sig").strip() or default
        except OSError:
            return default

    def _read_install_path(self) -> Path | None:
        try:
            path = Path(STATE_FILE.read_text(encoding="utf-8-sig").strip())
            if path.is_dir():
                return path
        except (OSError, ValueError):
            pass
        # One-click installation works on a fresh machine; users can still choose
        # another drive before downloading the multi-gigabyte client.
        return Path.home() / "Games"

    def _write_install_path(self, path: Path) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(str(path), encoding="utf-8")
        with self._lock:
            self._install_path = path
        self._refresh_install_root()

    def _refresh_install_root(self) -> None:
        with self._lock:
            selected = self._install_path
        if selected is None or not selected.exists():
            with self._lock:
                self._game_root = None
            return
        root = self._find_game_root(selected)
        with self._lock:
            self._game_root = root

    @staticmethod
    def _find_game_root(selected: Path) -> Path | None:
        selected = selected.resolve()
        candidates = [selected, *selected.parents]
        candidates.extend([
            selected / "GTA Criminal Russia",
            selected / "GTA Criminal Russia" / "GTA Criminal Russia",
        ])
        for candidate in candidates:
            if (candidate / "gta_sa.exe").is_file() and find_direct_game_client(candidate):
                return candidate
        # The folder picker can point at a parent folder. Locate the actual game
        # root by its game executable and the DLL the direct-connect helper loads.
        try:
            for runtime in selected.glob("*/multiplayer_c/grand_theft_auto_san_andreas.dll"):
                root = runtime.parents[1]
                if (root / "gta_sa.exe").is_file():
                    return root
            for runtime in selected.glob("*/grand_theft_auto_san_andreas.dll"):
                root = runtime.parent
                if (root / "gta_sa.exe").is_file():
                    return root
        except OSError:
            pass
        return selected if (
            (selected / "gta_sa.exe").is_file()
            and find_direct_game_client(selected)
        ) else None

    @staticmethod
    def _find_client_candidate(selected: Path | None) -> Path | None:
        """Find a partly installed game folder even if a required executable is missing."""
        if selected is None:
            return None
        candidates = (
            selected,
            selected / "GTA Criminal Russia",
            selected / "GTA Criminal Russia" / "GTA Criminal Russia",
        )
        for candidate in candidates:
            try:
                if not candidate.is_dir():
                    continue
                if candidate.name.casefold() == "gta criminal russia" or any(
                    (candidate / marker).exists()
                    for marker in ("gta_sa.exe", "multiplayer_c", "models", "data")
                ):
                    return candidate
            except OSError:
                continue
        return None

    def _auto_repair_allowed(self, root: Path, reason: str) -> bool:
        """Limit crash-triggered full archive repairs to one attempt per install/day."""
        resolved = str(Path(root).resolve())
        now = time.time()
        with self._lock:
            if self._busy:
                return False
            if self._process is not None and self._process.poll() is None:
                return False
            try:
                previous = json.loads(AUTO_REPAIR_STATE_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                previous = {}
            try:
                previous_attempt = float(previous.get("attempted_at") or 0)
            except (TypeError, ValueError):
                previous_attempt = 0.0
            same_install = str(previous.get("root") or "").casefold() == resolved.casefold()
            previous_result = str(previous.get("result") or "failed").casefold()
            cooldown = (
                AUTO_REPAIR_FAILURE_RETRY_SECONDS
                if previous_result == "failed"
                else AUTO_REPAIR_COOLDOWN_SECONDS
            )
            if same_install and now - previous_attempt < cooldown:
                remaining = max(0.0, cooldown - max(0.0, now - previous_attempt))
                retry_minutes = max(1, int((remaining + 59) // 60))
                if previous_result == "failed":
                    last_detail = str(previous.get("detail") or previous.get("reason") or reason)
                    diagnosis = diagnose_auto_repair_failure(
                        last_detail, str(previous.get("phase") or "repair")
                    )
                    diagnosis["retry_minutes"] = retry_minutes
                    message = (
                        f"Автовосстановление не удалось: {diagnosis['problem']}. "
                        f"Автоповтор через {retry_minutes} мин. "
                        "Подробности и исходная ошибка показаны ниже."
                    )
                else:
                    diagnosis = {}
                    message = (
                        "Автовосстановление уже запускалось за последние 24 часа. "
                        "Проверь журнал или нажми «Переустановить» для ручного повтора."
                    )
                write_launcher_log(
                    f"Automatic repair suppressed: root={resolved}; result={previous_result}; "
                    f"cooldown_seconds={cooldown}; reason={reason}; "
                    f"last_failure={previous.get('detail')!r}"
                )
                with self._lock:
                    self._phase = "error"
                    self._error = message
                    self._status = self._error
                    if diagnosis:
                        self._repair_failure = diagnosis
                    elif previous_result != "failed":
                        self._repair_failure = {}
                return False
            AUTO_REPAIR_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = AUTO_REPAIR_STATE_FILE.with_name(AUTO_REPAIR_STATE_FILE.name + ".tmp")
            temporary.write_text(
                json.dumps({
                    "root": resolved,
                    "attempted_at": now,
                    "reason": reason[:500],
                    "result": "in_progress",
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, AUTO_REPAIR_STATE_FILE)
        return True

    def _finish_auto_repair_attempt(
        self, root: Path, *, succeeded: bool, detail: str, phase: str = ""
    ) -> None:
        """Record whether a guarded automatic repair actually produced a verified client."""
        resolved = str(Path(root).resolve())
        try:
            payload = json.loads(AUTO_REPAIR_STATE_FILE.read_text(encoding="utf-8"))
            if str(payload.get("root") or "").casefold() != resolved.casefold():
                write_launcher_log(
                    f"Skipped auto-repair result update because the guard now points elsewhere: {resolved}"
                )
                return
            payload["result"] = "succeeded" if succeeded else "failed"
            payload["finished_at"] = time.time()
            payload["detail"] = str(detail or "")[:2400]
            payload["phase"] = str(phase or "repair")
            if succeeded:
                payload.pop("diagnostic", None)
                diagnosis = {}
            else:
                diagnosis = diagnose_auto_repair_failure(str(detail or ""), str(phase or "repair"))
                try:
                    attempted_at = float(payload.get("attempted_at") or time.time())
                except (ValueError, TypeError):
                    attempted_at = time.time()
                remaining = max(
                    0.0,
                    AUTO_REPAIR_FAILURE_RETRY_SECONDS - max(0.0, time.time() - attempted_at),
                )
                diagnosis["retry_minutes"] = max(0, int((remaining + 59) // 60))
                payload["diagnostic"] = diagnosis
            temporary = AUTO_REPAIR_STATE_FILE.with_name(AUTO_REPAIR_STATE_FILE.name + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, AUTO_REPAIR_STATE_FILE)
            with self._lock:
                self._repair_failure = diagnosis
            write_launcher_log(
                f"Automatic repair result recorded: root={resolved}; "
                f"result={payload['result']}; phase={payload['phase']}; "
                f"diagnostic={diagnosis!r}; detail={payload['detail']!r}"
            )
        except (OSError, ValueError, TypeError) as exc:
            write_launcher_log(f"Could not record automatic repair result for {resolved}: {exc!r}")

    def _request_auto_repair(self, root: Path, reason: str) -> bool:
        """Start one guarded full-client restore; never loop continuously on repeated crashes."""
        if not self._auto_repair_allowed(root, reason):
            write_launcher_log(
                f"Automatic repair unavailable or throttled: root={root}; reason={reason}"
            )
            return False
        write_launcher_log(
            f"Automatic client repair requested: root={root}; reason={reason}"
        )
        with self._lock:
            if self._game_root is None and (root / "gta_sa.exe").is_file():
                self._game_root = root
        result = self._begin_install(reinstall=True, auto_repair_root=root)
        if not result.get("ok"):
            message = str(result.get("message") or "Не удалось начать автовосстановление.")
            self._finish_auto_repair_attempt(root, succeeded=False, detail=message)
            write_launcher_log(f"Automatic client repair could not start: {message}")
            with self._lock:
                self._phase = "error"
                self._error = message
                self._status = self._error
            return False
        return True

    def _refresh_server(self) -> None:
        try:
            host, port = load_server_address(SERVER_FILE)
            endpoint = f"{host}:{port}"
            with self._lock:
                previous = self._endpoint
                self._endpoint = endpoint
            if endpoint != previous:
                write_launcher_log(f"Server endpoint refreshed from GitHub config: {endpoint}")
        except Exception as exc:
            write_launcher_log(f"Server config refresh failed: {exc!r}")

    def _server_monitor(self) -> None:
        """Refresh the public endpoint and poll the SA:MP query protocol in background."""
        next_config_refresh = 0.0
        last_online: bool | None = None
        while True:
            now = time.monotonic()
            if now >= next_config_refresh:
                self._refresh_server()
                next_config_refresh = now + 60.0
            with self._lock:
                endpoint = self._endpoint
            try:
                host, port = parse_server_address(endpoint)
                result = query_samp_server(host, port, timeout=0.8)
            except Exception as exc:
                result = {
                    "online": False,
                    "ping_ms": None,
                    "method": "none",
                    "detail": f"Не удалось проверить сервер: {exc}",
                }
            online = bool(result.get("online"))
            checked_at = time.strftime("%H:%M:%S")
            with self._lock:
                self._server_online = online
                self._server_ping_ms = result.get("ping_ms") if online else None
                self._server_method = str(result.get("method") or "none")
                self._server_detail = str(result.get("detail") or "")
                self._server_checked_at = checked_at
            if last_online is None or online != last_online:
                write_launcher_log(
                    f"Server query status changed: endpoint={endpoint}; online={online}; "
                    f"ping_ms={result.get('ping_ms')}; method={result.get('method')}; "
                    f"detail={result.get('detail')!r}"
                )
                last_online = online
            time.sleep(12.0)

    def _startup_check(self) -> None:
        with self._lock:
            self._checking_client = True
        try:
            with self._lock:
                root = self._game_root
                selected = self._install_path
            candidate = root or self._find_client_candidate(selected)
            if not candidate:
                with self._lock:
                    self._client_health = {
                        "healthy": False,
                        "root": "",
                        "issues": [],
                        "warnings": [],
                        "summary": "Игровая папка ещё не выбрана.",
                        "integrity_mode": "pending",
                    }
                    self._status = "Выбери папку игры или скачай клиент."
                    self._phase = "idle"
                return

            report = inspect_client_installation(
                candidate,
                progress_callback=lambda done, total, name: self._set_progress(
                    "integrity", done, total, f"Проверяю файлы: {name}"
                ),
            )
            with self._lock:
                self._client_health = report
            write_launcher_log(
                f"Client self-check: root={candidate}; healthy={report['healthy']}; "
                f"mode={report.get('integrity_mode')}; checked_hashes={report.get('checked_files')}; "
                f"issues={report.get('issues')!r}; warnings={report.get('warnings')!r}"
            )
            if report["healthy"]:
                # Inspect recent Windows crash records before enabling Play. This lets
                # the launcher address a crash that happened before this launcher run.
                recent = collect_windows_crash_events(
                    (
                        "gta_sa.exe",
                        "multiplayer_browser_cr.exe",
                        "grand_theft_auto_san_andreas.dll",
                        "LEDERG-DirectLaunch.exe",
                    ),
                    started_at=time.time() - 600.0,
                    attempts=1,
                )
                recent = select_client_crash_events(recent, candidate)
                if recent:
                    for event in recent:
                        write_launcher_log("Recent Windows crash event matched client: " + event)
                    faulting_module = extract_faulting_module(recent)
                    faulting_module_path = extract_faulting_module_path(recent)
                    backup = (
                        quarantine_faulting_optional_module(
                            candidate, faulting_module, faulting_module_path
                        )
                        if faulting_module and faulting_module_path
                        else None
                    )
                    if backup is not None:
                        report = inspect_client_installation(candidate, full_scan=False)
                        with self._lock:
                            self._client_health = report
                            self._game_root = candidate
                            self._status = (
                                "В журнале Windows найден сбой в локальном ENB d3d9.dll. "
                                f"Отключил его обратимым способом; резервная копия: {backup.name}. "
                                "Проверь запуск игры."
                            )
                            self._progress = 100.0
                            self._progress_label = "ENB ОТКЛЮЧЁН · BACKUP OK"
                            self._phase = "ready" if report["healthy"] else "error"
                            self._error = "" if report["healthy"] else "; ".join(report["issues"][:5])
                        write_launcher_log(
                            f"Proactive ENB workaround applied from recent Windows event: "
                            f"module={faulting_module}; path={faulting_module_path}; backup={backup}; "
                            f"health={report!r}"
                        )
                        return

                    reason = (
                        "Recent Windows Application crash for this install: "
                        f"module={faulting_module or 'unknown'}; "
                        f"path={faulting_module_path or 'unknown'}; "
                        + " | ".join(recent[:3])
                    )
                    if self._request_auto_repair(candidate, reason):
                        return
                    report = dict(report)
                    report["healthy"] = False
                    report["issues"] = list(report.get("issues") or []) + [
                        "В журнале Windows найден свежий ACCESS_VIOLATION для этой установки; "
                        "автоматическое восстановление ограничено защитой от повторных циклов."
                    ]
                    report["summary"] = "Найдена свежая авария Windows; нужна диагностика или ручное восстановление."
                    with self._lock:
                        self._client_health = report
                        self._status = report["summary"]
                        self._error = self._status
                        self._phase = "error"
                    return

                if report.get("integrity_mode") == "structural-only":
                    reason = (
                        "Legacy client has no trusted integrity inventory; "
                        "establish a SHA-256 baseline from the pinned official archive"
                    )
                    write_launcher_log(
                        f"Legacy client needs a trusted integrity baseline; starting one-time repair: "
                        f"root={candidate}"
                    )
                    if self._request_auto_repair(candidate, reason):
                        return
                    report = dict(report)
                    report["healthy"] = False
                    report["issues"] = list(report.get("issues") or []) + [
                        "Нет доверенного манифеста контрольных сумм; клиент нельзя считать полностью проверенным."
                    ]
                    report["summary"] = (
                        "Нужна полная проверка: автоматическое восстановление не запущено. "
                        "Открой журнал или нажми «Переустановить»."
                    )
                    with self._lock:
                        self._client_health = report
                        self._game_root = candidate
                        self._status = report["summary"]
                        self._error = self._status
                        self._phase = "error"
                    return

                with self._lock:
                    self._game_root = candidate if (candidate / "gta_sa.exe").is_file() else self._game_root
                    self._status = "Клиент проверен и готов к запуску."
                    self._progress = 100.0
                    self._progress_label = "АВТОПРОВЕРКА ПРОЙДЕНА"
                    self._phase = "ready"
                    self._error = ""
                return

            reason = "; ".join(report["issues"][:5])
            if self._request_auto_repair(candidate, reason or "client integrity check failed"):
                return
            with self._lock:
                self._status = (
                    "Обнаружены ошибки клиента, но автоматическое восстановление не запущено. "
                    "Открой журнал или нажми «Переустановить»."
                )
                self._error = self._status
                self._phase = "error"
        except Exception as exc:
            write_launcher_log("Client self-check failed: " + traceback.format_exc())
            with self._lock:
                self._status = f"Ошибка автопроверки клиента: {exc}"
                self._error = self._status
                self._phase = "error"
        finally:
            with self._lock:
                self._checking_client = False

    def get_state(self) -> dict:
        with self._lock:
            root = self._game_root
            selected = self._install_path
            busy = self._busy or self._checking_client
            phase = self._phase
            status = self._status
            progress = self._progress
            progress_label = self._progress_label
            current_file = self._current_file
            error = self._error
            endpoint = self._endpoint
            nickname = self._nickname
            server_online = self._server_online
            server_ping_ms = self._server_ping_ms
            server_method = self._server_method
            server_detail = self._server_detail
            server_checked_at = self._server_checked_at
            last_exit_code = self._last_exit_code
            health = dict(self._client_health)
            repair_failure = dict(getattr(self, "_repair_failure", {}) or {})
        runtime = find_direct_game_client(root) if root else None
        browser = find_client(root) if root else None
        client_candidate = root or self._find_client_candidate(selected)
        client_found = bool(client_candidate and client_candidate.is_dir())
        client_ready = bool(
            root
            and health.get("healthy")
            and (root / "gta_sa.exe").is_file()
            and runtime
        )
        display_path = str(root or client_candidate or selected) if (root or client_candidate or selected) else ""
        return {
            "client_ready": client_ready,
            "client_found": client_found,
            "install_path": display_path,
            "endpoint": endpoint,
            "nickname": nickname,
            "busy": busy,
            "phase": phase,
            "status": error or status,
            "progress": progress,
            "progress_label": progress_label,
            "current_file": current_file,
            "browser_found": bool(browser),
            "server_online": server_online,
            "server_ping_ms": server_ping_ms,
            "server_method": server_method,
            "server_detail": server_detail,
            "server_checked_at": server_checked_at,
            "last_exit_code": last_exit_code,
            "client_health_ok": bool(health.get("healthy")),
            "client_health_summary": str(health.get("summary") or ""),
            "client_health_issues": list(health.get("issues") or []),
            "client_health_warnings": list(health.get("warnings") or []),
            "client_integrity_mode": str(health.get("integrity_mode") or "pending"),
            "repair_failure": repair_failure,
        }

    def set_nickname(self, nickname: str) -> dict:
        clean = sanitize_nickname(nickname)
        with self._lock:
            self._nickname = clean
        try:
            NICK_FILE.write_text(clean, encoding="utf-8")
        except OSError as exc:
            write_launcher_log(f"Could not persist nickname: {exc!r}")
        return {"ok": True, "nickname": clean}

    def choose_folder(self) -> dict:
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                initial = str(self._install_path or Path.home() / "Documents")
                selected = filedialog.askdirectory(
                    parent=root,
                    title="Выбери папку игры или место установки LEDERG CR:MP",
                    initialdir=initial if Path(initial).is_dir() else str(Path.home()),
                    mustexist=True,
                )
            finally:
                root.destroy()
        except Exception as exc:
            return {"ok": False, "message": f"Не удалось открыть выбор папки: {exc}", "state": self.get_state()}
        if not selected:
            return {"ok": False, "message": "Выбор папки отменён.", "state": self.get_state()}
        try:
            path = Path(selected).resolve()
            self._write_install_path(path)
            state = self.get_state()
            if not state["client_ready"]:
                with self._lock:
                    self._status = "Папка выбрана. Нажми «Скачать / восстановить файлы»."
                    self._phase = "idle"
                state = self.get_state()
            return {"ok": True, "message": "Папка установки сохранена.", "state": state}
        except Exception as exc:
            return {"ok": False, "message": f"Путь установки не сохранён: {exc}", "state": self.get_state()}

    def open_log(self) -> dict:
        """Open the launcher diagnostics so users can share the exact crash context."""
        path = DATA_HOME / "launcher.log"
        try:
            DATA_HOME.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                write_launcher_log("Launcher log opened before any diagnostics were recorded.")
            os.startfile(str(path))
            return {"ok": True, "message": "Открыл журнал LEDERG CR:MP."}
        except Exception as exc:
            write_launcher_log(f"Could not open launcher log: {exc!r}")
            return {"ok": False, "message": f"Не удалось открыть журнал: {exc}"}

    def open_game_folder(self) -> dict:
        with self._lock:
            root = self._game_root or self._install_path
        if not root or not root.is_dir():
            return {"ok": False, "message": "Сначала выбери папку клиента."}
        try:
            os.startfile(str(root))
            return {"ok": True, "message": "Открыл папку игры."}
        except Exception as exc:
            return {"ok": False, "message": f"Не удалось открыть папку: {exc}"}

    def launch_game(self, nickname: str) -> dict:
        """Launch the bundled multiplayer browser and connect using the public IP config."""
        self.set_nickname(nickname)
        with self._lock:
            if self._busy:
                return {"ok": False, "message": "Дождись завершения текущей операции."}
            root = self._game_root
        if not root:
            return {
                "ok": False,
                "message": "Не найдена папка игры. Выбери папку или скачай клиент.",
            }

        # Refresh the public server file immediately before starting the browser,
        # not only in the background poller. Offline fallback uses the last good cache.
        self._refresh_server()
        with self._lock:
            endpoint = self._endpoint

        health = inspect_client_installation(
            root,
            progress_callback=lambda done, total, name: self._set_progress(
                "integrity", done, total, f"Проверяю перед запуском: {name}"
            ),
        )
        with self._lock:
            self._client_health = health
        if not health["healthy"]:
            reason = "; ".join(health["issues"][:5])
            write_launcher_log(
                f"Pre-launch check rejected the client: root={root}; issues={health['issues']!r}"
            )
            if self._request_auto_repair(root, reason or "pre-launch integrity failure"):
                return {
                    "ok": True,
                    "message": "Нашёл повреждённые файлы и запустил автоматическое восстановление.",
                }
            return {
                "ok": False,
                "message": (
                    "Проверка перед запуском обнаружила ошибки. "
                    "Автовосстановление ограничено защитой от повторных циклов; "
                    "используй «Переустановить» или открой журнал."
                ),
            }

        try:
            host, port = parse_server_address(endpoint)
            helper = direct_launcher_path().resolve()
            if not helper.is_file():
                raise FileNotFoundError(
                    "Не найден встроенный помощник LEDERG-DirectLaunch.exe. "
                    "Обнови или переустанови сам лаунчер."
                )

            command = build_direct_launcher_command(
                helper, root, host, port, self._nickname
            )
            game = Path(command[command.index("--game") + 1])
            multiplayer_dll = Path(command[command.index("--dll") + 1])
            try:
                helper_stat = helper.stat()
                helper_details = f"size={helper_stat.st_size}; mtime={helper_stat.st_mtime_ns}"
            except OSError as stat_error:
                helper_details = f"stat_error={stat_error!r}"

            write_launcher_log(
                f"Direct-connect launch requested: helper={helper}; {helper_details}; "
                f"game={game}; dll={multiplayer_dll}; cwd={root}; "
                f"endpoint={host}:{port}; nickname={self._nickname}"
            )
            launch_started_at = time.time()
            process = spawn_direct_launcher(command, root)
            if process is None:
                raise RuntimeError("Помощник прямого подключения не вернул дескриптор процесса.")

            with self._lock:
                self._process = process
                self._status = (
                    f"Запускаю игру без браузера. Сервер: {host}:{port}. PID помощника {process.pid}."
                )
                self._phase = "ready"
                self._error = ""
                self._progress = 100.0
                self._progress_label = "ПРЯМОЕ ПОДКЛЮЧЕНИЕ · CMD СКРЫТ"
            write_launcher_log(
                f"Direct-connect helper started: pid={process.pid}; "
                f"endpoint={host}:{port}; command={command!r}; browser_not_started=true"
            )
            self._start_xpanel_hotkey(process, root)
            threading.Thread(
                target=self._monitor_process,
                args=(process, launch_started_at),
                daemon=True,
                name="LEDERG-direct-game-process-monitor",
            ).start()
            return {
                "ok": True,
                "message": (
                    f"Запускаю GTA: San Andreas напрямую, без окна браузера и консоли. "
                    f"Адрес сервера: {host}:{port}."
                ),
            }
        except Exception as exc:
            write_launcher_log(
                f"ERROR launching direct CR:MP session: {traceback.format_exc()}"
            )
            with self._lock:
                self._phase = "error"
                self._error = str(exc)
                self._status = "Ошибка прямого запуска CR:MP."
            return {
                "ok": False,
                "message": f"Не удалось напрямую запустить/подключить CR:MP: {exc}",
            }

    def _start_xpanel_hotkey(self, process: subprocess.Popen, root: Path) -> None:
        """Start an X-key watcher tied to this game process; never global-type elsewhere."""
        previous = self._xpanel_stop
        if previous is not None:
            previous.set()
        stop_event = threading.Event()
        self._xpanel_stop = stop_event
        threading.Thread(
            target=self._watch_xpanel_hotkey,
            args=(process, root, stop_event),
            daemon=True,
            name="LEDERG-X-panel-hotkey",
        ).start()
        write_launcher_log(
            f"X hotkey watcher started for CR:MP PID {process.pid}; "
            "only sends /xpanel when that exact game process is foreground."
        )

    def _watch_xpanel_hotkey(
        self, process: subprocess.Popen, root: Path, stop_event: threading.Event
    ) -> None:
        was_down = False
        try:
            user32 = ctypes.windll.user32
            while not stop_event.wait(0.025):
                if process.poll() is not None:
                    break
                key_down = bool(int(user32.GetAsyncKeyState(VK_X)) & 0x8000)
                foreground_game_pid = _foreground_game_process_id(root, process.pid)
                if should_trigger_xpanel_hotkey(
                    key_down=key_down,
                    was_down=was_down,
                    foreground_pid=foreground_game_pid,
                    game_pid=foreground_game_pid,
                ):
                    write_launcher_log(
                        f"Physical X pressed in foreground CR:MP window (pid={foreground_game_pid}); "
                        f"launched runtime pid={process.pid}; sending /xpanel."
                    )
                    if not _type_xpanel_command(foreground_game_pid):
                        write_launcher_log(
                            f"Could not send /xpanel for game pid {process.pid}; "
                            "use the /xpanel chat command as a fallback."
                        )
                was_down = key_down
        except Exception:
            write_launcher_log("X hotkey watcher failed: " + traceback.format_exc())
        finally:
            if self._xpanel_stop is stop_event:
                self._xpanel_stop = None
            write_launcher_log(f"X hotkey watcher stopped for CR:MP PID {process.pid}.")

    def _monitor_process(
        self, process: subprocess.Popen, started_at: float | None = None
    ) -> None:
        try:
            code = process.wait()
            if self._xpanel_stop is not None and self._process is process:
                self._xpanel_stop.set()
            code_hex = f"0x{(int(code) & 0xFFFFFFFF):08X}"
            with self._lock:
                self._last_exit_code = int(code)
                if self._process is process:
                    self._process = None
                    if code == 0:
                        self._status = "Игровой клиент закрылся."
                        self._phase = "ready" if self.get_state()["client_ready"] else "idle"
                    else:
                        self._status = format_client_exit_code(code)
                        self._phase = "error"
                        self._error = self._status
            write_launcher_log(
                f"Native runtime exited: pid={process.pid}; code={code}; hex={code_hex}; "
                f"command={process.args!r}"
            )

            if (int(code) & 0xFFFFFFFF) == 0xC0000005:
                command = process.args
                command_items = [command] if isinstance(command, str) else list(command or [])
                executable_names = [
                    str(item).replace("\\", "/").rsplit("/", 1)[-1]
                    for item in command_items
                    if str(item).strip()
                ]
                executable_names.extend([
                    "LEDERG-DirectLaunch.exe",
                    "multiplayer_browser.cr.exe",
                    "gta_sa.exe",
                    "grand_theft_auto_san_andreas.dll",
                ])
                events = collect_windows_crash_events(
                    executable_names, started_at=started_at
                )
                if events:
                    for event in events:
                        write_launcher_log(event)
                    with self._lock:
                        if self._process is None and self._last_exit_code == int(code):
                            original = self._error or self._status
                            self._error = original.replace(
                                "Если повторится, нужен журнал Windows «Приложение» "
                                "с именем сбойного модуля.",
                                "Событие Windows записано в «ЖУРНАЛ» лаунчера.",
                            )
                            self._status = self._error
                    write_launcher_log(
                        "Windows crash-event diagnostics collected automatically; "
                        "see launcher.log for the faulting application/module."
                    )
                else:
                    write_launcher_log(
                        "No matching Windows Application Error/WER event was found automatically; "
                        "check Windows Event Viewer > Windows Logs > Application."
                    )

                with self._lock:
                    root = self._game_root or self._install_path
                faulting_module = extract_faulting_module(events)
                faulting_module_path = extract_faulting_module_path(events)
                if root and faulting_module and faulting_module_path:
                    backup = quarantine_faulting_optional_module(
                        root, faulting_module, faulting_module_path
                    )
                    if backup is not None:
                        health = inspect_client_installation(root, full_scan=False)
                        with self._lock:
                            self._client_health = health
                            if health["healthy"]:
                                self._phase = "ready"
                                self._error = ""
                                self._status = (
                                    "Windows указала локальный ENB d3d9.dll в аварийном событии. "
                                    f"Временно отключил его с резервной копией: {backup.name}. "
                                    "Запусти игру ещё раз."
                                )
                            else:
                                self._phase = "error"
                                self._error = (
                                    "Отключил подтверждённую ENB-обёртку, но проверка нашла другие ошибки. "
                                    + "; ".join(health["issues"][:4])
                                )
                                self._status = self._error
                        write_launcher_log(
                            f"Confirmed crash-module workaround applied: module={faulting_module}; "
                            f"backup={backup}; health={health!r}"
                        )
                        return

                reason = (
                    f"CR:MP access violation {code_hex}; "
                    f"Windows faulting module={faulting_module or 'not identified'}"
                )
                if root and self._request_auto_repair(root, reason):
                    write_launcher_log(
                        "Automatic full-client restore started after access violation. "
                        "It is rate-limited to one automatic attempt per install in 24 hours."
                    )
                else:
                    write_launcher_log(
                        "Automatic client restore was not started; inspect launcher.log and "
                        "Windows Application events, or use the manual reinstall action."
                    )
        except Exception as exc:
            write_launcher_log(f"Process monitor error: {exc!r}")

    def start_install(self) -> dict:
        return self._begin_install(reinstall=False)

    def reinstall_client(self) -> dict:
        """Re-download the official Drive archive and restore files over the installation."""
        return self._begin_install(reinstall=True)

    def _begin_install(
        self, *, reinstall: bool, auto_repair_root: Path | None = None
    ) -> dict:
        with self._lock:
            if self._busy:
                return {"ok": False, "message": "Установка уже выполняется."}
            if self._process is not None and self._process.poll() is None:
                return {
                    "ok": False,
                    "message": "Сначала полностью закрой CR:MP, затем запускай восстановление.",
                }
            selected = (self._game_root or self._install_path) if reinstall else self._install_path
            if selected is None:
                return {"ok": False, "message": "Сначала выбери папку установки."}
            self._busy = True
            self._phase = "manifest"
            self._status = (
                "Готовлю переустановку и проверку файлов…"
                if reinstall else "Готовлю установку игрового клиента…"
            )
            self._progress = 0.0
            self._progress_label = "ПЕРЕУСТАНОВКА" if reinstall else "ПОДГОТОВКА"
            self._error = ""
            self._repair_failure = {}
        write_launcher_log(
            f"Client {'reinstall/repair' if reinstall else 'install'} requested: selected={selected}"
        )
        threading.Thread(
            target=self._install_worker,
            args=(selected, reinstall, auto_repair_root),
            daemon=True,
            name="LEDERG-reinstall" if reinstall else "LEDERG-install",
        ).start()
        return {
            "ok": True,
            "message": "Переустановка и восстановление файлов запущены."
            if reinstall else "Установка клиента запущена.",
        }

    def _set_progress(self, phase: str, done: int, total: int, label: str) -> None:
        with self._lock:
            self._phase = phase
            self._status = label or "Обрабатываю файлы клиента…"
            self._current_file = label or ""
            self._progress = (min(100.0, max(0.0, float(done) * 100.0 / float(total))) if total else 0.0)
            if phase == "download":
                self._progress_label = "ЗАГРУЗКА ФАЙЛОВ"
            elif phase == "extract":
                self._progress_label = "РАСПАКОВКА АРХИВА"
            else:
                self._progress_label = "ПРОВЕРКА ФАЙЛОВ"

    def _install_worker(
        self,
        selected: Path,
        reinstall: bool = False,
        auto_repair_root: Path | None = None,
    ) -> None:
        archive: Path | None = None
        repair_succeeded = False
        failure_detail = ""
        failure_phase = ""
        integrity_inventory = DATA_HOME / "pending-client-integrity.json"
        try:
            with self._lock:
                self._status = (
                    "Загружаю архив заново для восстановления клиента…"
                    if reinstall else "Обновляю конфигурацию сервера…"
                )
                self._phase = "manifest"
            if reinstall:
                write_launcher_log(
                    f"Reinstall mode active: existing game files will be overwritten from Drive; root={selected}"
                )
            try:
                release = fetch_release_manifest()
            except Exception as exc:
                write_launcher_log(f"Release manifest unavailable; using last-resort client URLs: {exc!r}")
                release = {
                    "version": "fallback",
                    "archive_url": ZIP_URL,
                    "files_manifest_url": CLIENT_FILES_MANIFEST_URL,
                    "archive_size": 0,
                    "sha256": "",
                    "client_executable": DEFAULT_CLIENT_EXECUTABLE,
                    "launch_mode": DEFAULT_LAUNCH_MODE,
                }

            # The full client payload is intentionally sourced only from the
            # pinned Google Drive archive. Do not contact Cloudpub or a per-file manifest.
            manifest_url = ""
            manifest = None
            write_launcher_log(
                f"Drive-only client source selected: {ZIP_URL}; "
                f"release metadata version={release.get('version', 'unknown')}."
            )
            if manifest is None:
                install_name = "GTA Criminal Russia"
                selected_is_game_root = (
                    (selected / "gta_sa.exe").is_file()
                    or selected.name.casefold() == install_name.casefold()
                    or any((selected / marker).exists() for marker in ("multiplayer_c", "models", "data"))
                )
                destination = selected.parent if selected_is_game_root else selected
                archive_url = release.get("archive_url") or ZIP_URL
                with self._lock:
                    self._status = "Скачиваю игровой клиент из Google Drive…"
                    self._phase = "download"
                    self._progress = 0.0
                    self._progress_label = "ЗАГРУЗКА АРХИВА"
                archive = download_archive(
                    destination,
                    progress=self._set_progress,
                    url=archive_url,
                    expected_sha256=release.get("sha256", ""),
                    expected_size=int(release.get("archive_size", 0) or 0),
                )
                integrity_inventory.unlink(missing_ok=True)
                count = safe_extract_zip(
                    archive,
                    destination,
                    progress=lambda done, total, name: self._set_progress("extract", done, total, name),
                    integrity_manifest_path=integrity_inventory,
                )
                client = find_client(destination, release.get("client_executable"))
                if not client:
                    raise RuntimeError(
                        "Архив распакован, но браузер CR:MP не найден. "
                        "Проверь, что ZIP содержит папку multiplayer_c и игровой клиент."
                    )
                root = game_root_for(client)
                runtime = find_direct_game_client(root)
                if not (root / "gta_sa.exe").is_file() or not runtime:
                    raise RuntimeError(
                        "В архиве не найден игровой runtime grand_theft_auto_san_andreas.dll. "
                        "Нужен полный CR:MP-клиент, не только браузер серверов."
                    )
                prior_enb_backups = list(root.glob("d3d9.dll.lederg-disabled*.bak"))
                if prior_enb_backups and (root / "d3d9.dll").is_file():
                    restored_proxy_backup = quarantine_faulting_optional_module(
                        root, "d3d9.dll", str(root / "d3d9.dll")
                    )
                    if restored_proxy_backup is not None:
                        write_launcher_log(
                            "Kept previously confirmed ENB proxy disabled after archive repair; "
                            f"new backup={restored_proxy_backup}"
                        )
                finalize_client_integrity_manifest(root, destination, integrity_inventory)
                health = inspect_client_installation(root, full_scan=False)
                if not health["healthy"]:
                    raise RuntimeError(
                        "После распаковки автопроверка обнаружила проблемы: "
                        + "; ".join(health["issues"][:8])
                    )
                try:
                    archive.unlink(missing_ok=True)
                except OSError:
                    pass
                integrity_inventory.unlink(missing_ok=True)
                self._write_install_path(root)
                with self._lock:
                    self._game_root = root
                    self._install_path = root
                    self._client_health = health
                    self._progress = 100.0
                    self._progress_label = "ПРОВЕРКА ПРОЙДЕНА"
                    self._status = (
                        f"Клиент установлен и проверен ({count} файлов). "
                        f"{health['summary']}"
                    )
                    self._phase = "ready"
                    self._error = ""
                write_launcher_log(
                    f"Google Drive client installation completed and verified: "
                    f"root={root}; files={count}; health={health!r}"
                )
                repair_succeeded = True
                return

            install_name = str(manifest.get("install_root", "GTA Criminal Russia"))
            if (selected / "gta_sa.exe").is_file() or selected.name.casefold() == install_name.casefold():
                destination = selected.parent
            else:
                destination = selected
            with self._lock:
                self._status = "Скачиваю и проверяю игровые файлы…"
                self._phase = "download"
            _install_root, count = download_client_folder(
                destination,
                manifest_url,
                progress=self._set_progress,
                http=HTTP,
                manifest_payload=manifest,
            )
            new_install_root = destination / manifest["install_root"]
            if not new_install_root.is_dir():
                client = find_client(destination, release.get("client_executable"))
                new_install_root = game_root_for(client) if client else new_install_root
            client = find_client(new_install_root, release.get("client_executable"))
            if not client:
                raise RuntimeError(
                    "Файлы обновлены, но клиент не найден. Проверь манифест и повтори восстановление."
                )
            root = game_root_for(client)
            runtime = find_direct_game_client(root)
            if not (root / "gta_sa.exe").is_file() or not runtime:
                raise RuntimeError(
                    "Не найден игровой runtime grand_theft_auto_san_andreas.dll. "
                    "Манифест может описывать не полный клиент."
                )
            apply_successful_update(root, manifest)
            self._write_install_path(root)
            with self._lock:
                self._game_root = root
                self._install_path = root
                self._progress = 100.0
                self._progress_label = "ГОТОВО"
                self._status = f"Клиент {manifest.get('version', release.get('version', 'unknown'))} проверен."
                self._phase = "ready"
                self._error = ""
            write_launcher_log(f"Client update completed: root={root}; files={count}")
            repair_succeeded = True
        except Exception as exc:
            failure_detail = str(exc) or repr(exc)
            with self._lock:
                failure_phase = self._phase or "repair"
            diagnosis = diagnose_auto_repair_failure(failure_detail, failure_phase)
            write_launcher_log(
                f"ERROR updating client: phase={failure_phase}; "
                f"diagnosis={diagnosis!r}; traceback={traceback.format_exc()}"
            )
            with self._lock:
                self._phase = "error"
                self._repair_failure = diagnosis
                self._error = f"Восстановление не удалось: {diagnosis['problem']}"
                self._status = self._error
        finally:
            try:
                integrity_inventory.unlink(missing_ok=True)
                integrity_inventory.with_name(integrity_inventory.name + ".tmp").unlink(missing_ok=True)
            except OSError:
                pass
            if auto_repair_root is not None:
                with self._lock:
                    detail = failure_detail or self._error or self._status
                    phase = failure_phase or self._phase or "repair"
                self._finish_auto_repair_attempt(
                    auto_repair_root,
                    succeeded=repair_succeeded,
                    detail=detail,
                    phase=phase,
                )
            with self._lock:
                self._busy = False


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _fetch_live_web_page() -> tuple[bytes, str]:
    """Fetch the GitHub-published cover and verify it against the signed-by-HTTPS manifest."""
    base = "https://raw.githubusercontent.com/LEDERG777-TEXNARIC/lederg-luncher-crmp/main/"
    request = urllib.request.Request(
        base + "runtime-manifest.json",
        headers={"User-Agent": "LEDERG-CRMP-Launcher/2"},
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
    if payload.get("schema") != 1 or not isinstance(payload.get("files"), list):
        raise ValueError("Invalid live UI manifest")
    entry = next(
        (item for item in payload["files"] if item.get("path") == "web/index.html"),
        None,
    )
    if not isinstance(entry, dict):
        raise ValueError("Live UI is missing from the manifest")
    expected_size = entry.get("size")
    expected_hash = entry.get("sha256")
    if not isinstance(expected_size, int) or not 0 <= expected_size <= 20 * 1024 * 1024:
        raise ValueError("Invalid live UI size")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise ValueError("Invalid live UI SHA-256")
    request = urllib.request.Request(
        base + "web/index.html",
        headers={"User-Agent": "LEDERG-CRMP-Launcher/2"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(20 * 1024 * 1024 + 1)
    if len(data) != expected_size or _sha256_bytes(data) != expected_hash:
        raise ValueError("Live UI failed size/SHA-256 verification")
    return data, expected_hash


def _live_ui_updater(window, initial_page: Path) -> None:
    """Poll GitHub while the launcher is open and hot-reload only a verified HTML cover."""
    current_hash = ""
    try:
        current_hash = _sha256_bytes(initial_page.read_bytes())
    except OSError:
        pass
    live_root = DATA_HOME / "live-web"
    while True:
        time.sleep(60)
        try:
            data, digest = _fetch_live_web_page()
            if digest == current_hash:
                continue
            version_dir = live_root / digest
            version_dir.mkdir(parents=True, exist_ok=True)
            page = version_dir / "index.html"
            if not page.is_file() or _sha256_bytes(page.read_bytes()) != digest:
                temporary = version_dir / "index.html.tmp"
                temporary.write_bytes(data)
                temporary.replace(page)
            # Reload only after the replacement has passed integrity checks.
            window.load_url(page.resolve().as_uri())
            current_hash = digest
            write_launcher_log(f"Live launcher cover updated from GitHub: sha256={digest}")
        except Exception as exc:
            write_launcher_log(f"Live cover check failed; keeping current UI: {exc!r}")


def start() -> None:
    import webview

    DATA_HOME.mkdir(parents=True, exist_ok=True)
    api = LauncherApi()
    page = WEB_ROOT / "index.html"
    if not page.is_file():
        raise FileNotFoundError(f"Не найден HTML интерфейса лаунчера: {page}")
    window = webview.create_window(
        APP_NAME,
        url=page.resolve().as_uri(),
        js_api=api,
        width=1160,
        height=760,
        min_size=(900, 620),
        background_color="#080808",
        text_select=True,
    )
    threading.Thread(
        target=_live_ui_updater,
        args=(window, page),
        daemon=True,
        name="LEDERG-live-cover-updater",
    ).start()
    webview.start(debug=False)
