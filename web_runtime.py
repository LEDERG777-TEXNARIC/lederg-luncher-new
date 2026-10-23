from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
import tkinter as tk
import traceback
import urllib.parse
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
    ZIP_SIZE,
    ZIP_URL,
    build_direct_launcher_command,
    build_launch_command,
    direct_launcher_path,
    find_child_game_pid,
    is_process_running,
    download_archive,
    download_client_folder,
    ensure_install_space,
    ensure_legacy_directx_dependency,
    register_samp_game,
    fetch_release_manifest,
    find_client,
    find_injected_multiplayer_dll,
    game_root_for,
    load_server_address,
    parse_server_address,
    safe_extract_zip,
    should_use_manifest_update,
    sanitize_nickname,
    write_launcher_log,
)

DATA_HOME = Path(
    os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
) / "LEDERG CRMP"
WEB_ROOT = Path(os.environ.get("LEDERG_WEB_ROOT", Path(__file__).resolve().parent / "web"))
STATE_FILE = DATA_HOME / "install_location.txt"
NICK_FILE = DATA_HOME / "nickname.txt"

SERVER_FILE = DATA_HOME / "server_address.txt"

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
        self._phase = "idle"
        self._status = "Проверяю папку клиента…"
        self._progress = 0.0
        self._bytes_done = 0
        self._bytes_total = 0
        self._speed_bps = 0.0
        self._progress_last_at = 0.0
        self._progress_last_done = 0
        self._progress_label = "ПРОВЕРКА"
        self._current_file = ""
        self._error = ""
        self._process: subprocess.Popen | None = None
        self._game_pid: int | None = None
        self._xpanel_stop: threading.Event | None = None
        self._game_root: Path | None = None
        self._endpoint = f"{SERVER_HOST}:{SERVER_PORT}"
        self._nickname = self._read_text(NICK_FILE, "LEDERG")
        self._install_path = self._read_install_path()
        self._refresh_install_root()
        threading.Thread(target=self._refresh_server, daemon=True, name="LEDERG-server-config").start()
        threading.Thread(target=self._startup_check, daemon=True, name="LEDERG-client-check").start()

    @staticmethod
    def _read_text(path: Path, default: str) -> str:
        try:
            return path.read_text(encoding="utf-8-sig").strip() or default
        except OSError:
            return default

    def _read_install_path(self) -> Path | None:
        try:
            path = Path(STATE_FILE.read_text(encoding="utf-8-sig").strip())
            return path if path.is_dir() else None
        except (OSError, ValueError):
            return None

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
            if (candidate / "gta_sa.exe").is_file() and find_injected_multiplayer_dll(candidate):
                return candidate
        # The folder picker can point at a parent folder. Locate the actual game
        # root by its game executable and the DLL the direct-connect helper loads.
        try:
            for runtime in selected.glob("*/multiplayer_c/san_andreas_multiplayer.dll"):
                root = runtime.parents[1]
                if (root / "gta_sa.exe").is_file():
                    return root
            for runtime in selected.glob("*/san_andreas_multiplayer.dll"):
                root = runtime.parent
                if (root / "gta_sa.exe").is_file():
                    return root
        except OSError:
            pass
        return selected if (
            (selected / "gta_sa.exe").is_file()
            and find_injected_multiplayer_dll(selected)
        ) else None

    def _refresh_server(self) -> None:
        try:
            host, port = load_server_address(SERVER_FILE)
            with self._lock:
                self._endpoint = f"{host}:{port}"
        except Exception as exc:
            write_launcher_log(f"Server config refresh failed: {exc!r}")

    def _startup_check(self) -> None:
        try:
            with self._lock:
                root = self._game_root
            if root and find_injected_multiplayer_dll(root):
                with self._lock:
                    self._status = "Клиент готов к запуску."
                    self._progress = 100.0
                    self._progress_label = "КЛИЕНТ ПРОВЕРЕН"
                    self._phase = "ready"
            else:
                with self._lock:
                    self._status = "Выбери папку с игрой или скачай клиент."
                    self._phase = "idle"
        except Exception as exc:
            with self._lock:
                self._status = f"Проверка клиента: {exc}"
                self._phase = "error"

    def get_state(self) -> dict:
        with self._lock:
            root = self._game_root
            selected = self._install_path
            busy = self._busy
            phase = self._phase
            status = self._status
            progress = self._progress
            progress_label = self._progress_label
            bytes_done = self._bytes_done
            bytes_total = self._bytes_total
            speed_bps = self._speed_bps
            current_file = self._current_file
            error = self._error
            endpoint = self._endpoint
            nickname = self._nickname
        runtime = find_injected_multiplayer_dll(root) if root else None
        browser = find_client(root) if root else None
        client_ready = bool(root and (root / "gta_sa.exe").is_file() and runtime)
        display_path = str(root or selected) if (root or selected) else ""
        return {
            "client_ready": client_ready,
            "install_path": display_path,
            "endpoint": endpoint,
            "nickname": nickname,
            "busy": busy,
            "phase": phase,
            "status": error or status,
            "progress": progress,
            "progress_label": progress_label,
            "bytes_done": bytes_done,
            "bytes_total": bytes_total,
            "speed_bps": round(speed_bps, 1),
            "eta_seconds": int(max(0, bytes_total - bytes_done) / speed_bps) if speed_bps > 1 else None,
            "current_file": current_file,
            "browser_found": bool(browser),
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
        self.set_nickname(nickname)
        with self._lock:
            if self._busy:
                return {"ok": False, "message": "Дождись завершения текущей операции."}
            tracked_game_pid = self._game_pid
            active_process = self._process
            root = self._game_root
            endpoint = self._endpoint
        if tracked_game_pid and is_process_running(tracked_game_pid):
            return {"ok": False, "message": "CR:MP уже запущен."}
        if active_process is not None and active_process.poll() is None:
            return {"ok": False, "message": "Запуск CR:MP уже выполняется."}
        if not root:
            return {"ok": False, "message": "Не найдена папка игры. Выбери папку или скачай клиент."}

        game = root / "gta_sa.exe"
        multiplayer_dll = find_injected_multiplayer_dll(root)
        client = find_client(root, DEFAULT_CLIENT_EXECUTABLE)
        if not game.is_file() or not multiplayer_dll or client is None:
            return {
                "ok": False,
                "message": (
                    "Не найден полный CR:MP-клиент: нужны gta_sa.exe, "
                    "multiplayer_c/multiplayer_browser_cr.exe и multiplayer DLL. "
                    "Нажми «Скачать / восстановить файлы»."
                ),
            }

        process = None
        try:
            host, port = parse_server_address(endpoint)
            # Always let the supplied client bootstrap initialize game type and
            # Multiplayer adaptation. Injecting san_andreas_multiplayer.dll into
            # gta_sa.exe bypasses that initialization and produces the reported error.
            registered = register_samp_game(root, self._nickname)
            dependency = ensure_legacy_directx_dependency(root)
            command = build_launch_command(
                client, host, port, self._nickname, "samp-cli"
            )
            write_launcher_log(
                f"Starting supported CR:MP bootstrapper: executable={client}; "
                f"game={game}; multiplayer_dll={multiplayer_dll}; DirectX={dependency}; "
                f"registry_game={registered['gta_sa_exe']}; cwd={root}; "
                f"endpoint={host}:{port}; nickname={self._nickname}; command={command!r}"
            )
            process = subprocess.Popen(
                command,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )

            # multiplayer_browser_cr.exe creates the actual runtime process and may
            # then exit normally. Capture its child PID before starting monitoring.
            game_pid = None
            deadline = time.monotonic() + 7.0
            while time.monotonic() < deadline:
                candidate = find_child_game_pid(process.pid)
                if candidate and is_process_running(candidate):
                    game_pid = candidate
                    break
                time.sleep(0.1)

            if process.poll() is not None and game_pid is None:
                raise RuntimeError(
                    "Штатный загрузчик CR:MP закрылся, но игровой процесс не появился. "
                    f"Код выхода: {process.returncode}. Проверь файлы клиента."
                )

            with self._lock:
                self._process = process
                self._game_pid = game_pid
                self._status = (
                    f"CR:MP запущен — {host}:{port}; игровой PID "
                    f"{game_pid if game_pid else 'ожидание'}."
                )
                self._phase = "ready"
                self._error = ""
            write_launcher_log(
                f"CR:MP bootstrap started; bootstrap_pid={process.pid}; "
                f"game_pid={game_pid}; endpoint={host}:{port}."
            )
            self._start_xpanel_hotkey(process, root, game_pid)
            threading.Thread(
                target=self._monitor_process,
                args=(process, root, game_pid),
                daemon=True,
                name="LEDERG-game-monitor",
            ).start()
            return {
                "ok": True,
                "message": (
                    f"Запускаю штатный CR:MP и подключаю к серверу {host}:{port}."
                    if game_pid
                    else f"Штатный CR:MP запущен; ожидаю игровой процесс для подключения к {host}:{port}."
                ),
            }
        except Exception as exc:
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
            write_launcher_log(f"ERROR launching game: {traceback.format_exc()}")
            with self._lock:
                self._process = None
                self._game_pid = None
                self._phase = "error"
                self._error = str(exc)
                self._status = "Ошибка запуска CR:MP."
            return {"ok": False, "message": f"Не удалось запустить CR:MP: {exc}"}

    def _start_xpanel_hotkey(
        self, process: subprocess.Popen, root: Path, game_pid: int | None = None
    ) -> None:
        """Keep the X hotkey alive after the bootstrapper hands off to the game."""
        previous = self._xpanel_stop
        if previous is not None:
            previous.set()
        stop_event = threading.Event()
        self._xpanel_stop = stop_event
        threading.Thread(
            target=self._watch_xpanel_hotkey,
            args=(process, root, stop_event, game_pid),
            daemon=True,
            name="LEDERG-X-panel-hotkey",
        ).start()
        write_launcher_log(
            f"X hotkey watcher started; bootstrap PID={process.pid}; game PID={game_pid}."
        )

    def _watch_xpanel_hotkey(
        self,
        process: subprocess.Popen,
        root: Path,
        stop_event: threading.Event,
        game_pid: int | None = None,
    ) -> None:
        was_down = False
        try:
            user32 = ctypes.windll.user32
            tracked_game_pid = game_pid
            while not stop_event.wait(0.025):
                if tracked_game_pid is None:
                    tracked_game_pid = find_child_game_pid(process.pid)
                if process.poll() is not None and not is_process_running(tracked_game_pid):
                    break
                key_down = bool(int(user32.GetAsyncKeyState(VK_X)) & 0x8000)
                foreground_game_pid = _foreground_game_process_id(root, process.pid)
                if should_trigger_xpanel_hotkey(
                    key_down=key_down,
                    was_down=was_down,
                    foreground_pid=foreground_game_pid,
                    game_pid=tracked_game_pid,
                ):
                    write_launcher_log(
                        f"Physical X pressed in CR:MP window (game pid={tracked_game_pid}); "
                        "sending /xpanel."
                    )
                    if not _type_xpanel_command(tracked_game_pid):
                        write_launcher_log(
                            f"Could not send /xpanel for game pid {tracked_game_pid}; "
                            "use the /xpanel chat command as a fallback."
                        )
                was_down = key_down
        except Exception:
            write_launcher_log("X hotkey watcher failed: " + traceback.format_exc())
        finally:
            if self._xpanel_stop is stop_event:
                self._xpanel_stop = None
            write_launcher_log(f"X hotkey watcher stopped for CR:MP bootstrap PID {process.pid}.")

    def _monitor_process(
        self, process: subprocess.Popen, root: Path, game_pid: int | None = None
    ) -> None:
        try:
            code = process.wait()
            tracked_game_pid = game_pid
            if tracked_game_pid is None:
                deadline = time.monotonic() + 7.0
                while time.monotonic() < deadline:
                    candidate = find_child_game_pid(process.pid)
                    if candidate and is_process_running(candidate):
                        tracked_game_pid = candidate
                        break
                    time.sleep(0.1)
            if tracked_game_pid and is_process_running(tracked_game_pid):
                with self._lock:
                    if self._process is process:
                        self._game_pid = tracked_game_pid
                write_launcher_log(
                    f"CR:MP bootstrap exited with code={code}; waiting for game PID={tracked_game_pid}."
                )
                while is_process_running(tracked_game_pid):
                    time.sleep(0.5)
            if self._xpanel_stop is not None and self._process is process:
                self._xpanel_stop.set()
            with self._lock:
                if self._process is process:
                    self._process = None
                    self._game_pid = None
                    if code == 0:
                        self._status = "Игровой клиент закрылся."
                        self._phase = "ready" if self.get_state()["client_ready"] else "idle"
                    else:
                        self._status = f"Клиент завершился с кодом {code}. Смотри журнал."
                        self._phase = "error"
                        self._error = self._status
            write_launcher_log(
                f"CR:MP bootstrap/game monitoring finished: bootstrap_code={code}; "
                f"game_pid={tracked_game_pid}."
            )
        except Exception as exc:
            write_launcher_log(f"Process monitor error: {exc!r}")

    def start_install(self) -> dict:
        with self._lock:
            if self._busy:
                return {"ok": False, "message": "Установка уже выполняется."}
            selected = self._install_path
            if selected is None:
                return {"ok": False, "message": "Сначала нажми «Выбрать» и укажи папку установки."}
            self._busy = True
            self._phase = "manifest"
            self._status = "Получаю проверенный манифест клиента…"
            self._progress = 0.0
            self._bytes_done = 0
            self._bytes_total = 0
            self._speed_bps = 0.0
            self._progress_last_at = time.monotonic()
            self._progress_last_done = 0
            self._progress_label = "ПОДГОТОВКА"
            self._error = ""
        threading.Thread(target=self._install_worker, args=(selected,), daemon=True, name="LEDERG-install").start()
        return {"ok": True, "message": "Проверка и установка клиента запущены."}

    def _set_progress(self, phase: str, done: int, total: int, label: str) -> None:
        now = time.monotonic()
        done = max(0, int(done))
        total = max(0, int(total))
        with self._lock:
            self._phase = phase
            self._status = label or "Обрабатываю файлы клиента…"
            self._current_file = label or ""
            self._progress = (min(100.0, max(0.0, float(done) * 100.0 / float(total))) if total else 0.0)
            self._bytes_done = done if phase == "download" else 0
            self._bytes_total = total if phase == "download" else 0
            if phase != "download":
                self._speed_bps = 0.0
                self._progress_last_at = now
                self._progress_last_done = done
            else:
                if done < self._progress_last_done:
                    self._speed_bps = 0.0
                    self._progress_last_at = now
                    self._progress_last_done = done
                else:
                    elapsed = now - self._progress_last_at
                    if elapsed >= 0.35:
                        instant = max(0.0, (done - self._progress_last_done) / elapsed)
                        self._speed_bps = instant if self._speed_bps <= 0 else (self._speed_bps * 0.35 + instant * 0.65)
                        self._progress_last_at = now
                        self._progress_last_done = done
            if phase == "download":
                self._progress_label = "ЗАГРУЗКА ФАЙЛОВ"
            elif phase == "extract":
                self._progress_label = "РАСПАКОВКА АРХИВА"
            else:
                self._progress_label = "ПРОВЕРКА ФАЙЛОВ"

    def _install_worker(self, selected: Path) -> None:
        archive: Path | None = None
        try:
            with self._lock:
                self._status = "Обновляю конфигурацию сервера…"
                self._phase = "manifest"
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

            manifest_url = release.get("files_manifest_url") or CLIENT_FILES_MANIFEST_URL
            manifest = None
            try:
                manifest = fetch_files_manifest(manifest_url, http=HTTP)
            except Exception as exc:
                write_launcher_log(f"Client file manifest unavailable; ZIP route will be used: {exc!r}")

            install_name = str((manifest or {}).get("install_root", release.get("install_root", "GTA Criminal Russia")))
            selected_is_game_root = (selected / "gta_sa.exe").is_file()
            destination = (
                selected.parent
                if selected_is_game_root or selected.name.casefold() == install_name.casefold()
                else selected
            )
            use_manifest_update = (
                manifest is not None
                and should_use_manifest_update(destination, release.get("client_executable"))
            )

            if not use_manifest_update:
                # A clean install uses the smaller compressed archive and eight
                # resumable Range streams. The next launch can reconcile it with
                # the per-file manifest without re-downloading unchanged assets.
                with self._lock:
                    self._status = "Первичная установка: ускоренная загрузка ZIP…"
                    self._phase = "download"
                    self._progress = 0.0
                    self._bytes_done = 0
                    self._bytes_total = 0
                    self._speed_bps = 0.0
                    self._progress_last_at = time.monotonic()
                    self._progress_last_done = 0
                    self._progress_label = "8-WAY RANGE"
                archive_url = release.get("archive_url") or ZIP_URL
                ensure_install_space(
                    destination,
                    int(release.get("archive_size", 0) or ZIP_SIZE),
                    int(manifest.get("total_bytes", 0) or 0) if manifest else 0,
                )
                archive = download_archive(
                    destination,
                    progress=self._set_progress,
                    url=archive_url,
                    expected_sha256=release.get("sha256", ""),
                    expected_size=int(release.get("archive_size", 0) or 0),
                )
                count = safe_extract_zip(
                    archive,
                    destination,
                    progress=lambda done, total, name: self._set_progress("extract", done, total, name),
                )
                client = find_client(destination, release.get("client_executable"))
                if not client:
                    raise RuntimeError(
                        "Архив распакован, но CR:MP-клиент не найден. "
                        "Проверь, что ZIP содержит GTA Criminal Russia и multiplayer_c."
                    )
                root = game_root_for(client)
                runtime = find_injected_multiplayer_dll(root)
                if not (root / "gta_sa.exe").is_file() or not runtime:
                    raise RuntimeError(
                        "В архиве не найден игровой runtime san_andreas_multiplayer.dll. "
                        "Нужен полный CR:MP-клиент."
                    )
                archive.unlink(missing_ok=True)
                self._write_install_path(root)
                with self._lock:
                    self._game_root = root
                    self._install_path = root
                    self._progress = 100.0
                    self._progress_label = "ГОТОВО"
                    self._status = (
                        f"Первичная установка завершена ({count} файлов). "
                        "Последующие проверки смогут скачивать только изменённые файлы."
                    )
                    self._phase = "ready"
                    self._error = ""
                    self._speed_bps = 0.0
                write_launcher_log(
                    f"Client ZIP install completed: root={root}; files={count}; "
                    f"release={release.get('version', 'unknown')}"
                )
                return

            with self._lock:
                self._status = "Найден установленный клиент. Сверяю манифест и докачиваю только изменения…"
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
            runtime = find_injected_multiplayer_dll(root)
            if not (root / "gta_sa.exe").is_file() or not runtime:
                raise RuntimeError(
                    "Не найден игровой runtime san_andreas_multiplayer.dll. "
                    "Манифест может описывать не полный клиент."
                )
            apply_successful_update(root, manifest)
            self._write_install_path(root)
            with self._lock:
                self._game_root = root
                self._install_path = root
                self._progress = 100.0
                self._progress_label = "ГОТОВО"
                self._status = (
                    f"Клиент {manifest.get('version', release.get('version', 'unknown'))} проверен. "
                    "Скачаны только отличающиеся файлы."
                )
                self._phase = "ready"
                self._error = ""
                self._speed_bps = 0.0
            write_launcher_log(
                f"Client delta update completed: root={root}; files={count}; "
                f"fingerprint={manifest.get('fingerprint', '')}"
            )
        except Exception as exc:
            write_launcher_log(f"ERROR updating client: {traceback.format_exc()}")
            with self._lock:
                self._phase = "error"
                self._error = str(exc)
                self._status = f"Ошибка обновления: {exc}"
        finally:
            with self._lock:
                self._busy = False


def start() -> None:
    import webview

    DATA_HOME.mkdir(parents=True, exist_ok=True)
    api = LauncherApi()
    page = WEB_ROOT / "index.html"
    if not page.is_file():
        raise FileNotFoundError(f"Не найден HTML интерфейса лаунчера: {page}")
    webview.create_window(
        APP_NAME,
        url=page.resolve().as_uri(),
        js_api=api,
        width=1160,
        height=760,
        min_size=(900, 620),
        background_color="#080808",
        text_select=True,
    )
    webview.start(debug=False)
