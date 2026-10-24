from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from web_runtime import LauncherApi, spawn_direct_launcher


class DirectLaunchTests(unittest.TestCase):
    @staticmethod
    def make_api(root: Path) -> LauncherApi:
        api = object.__new__(LauncherApi)
        api._lock = threading.RLock()
        api._busy = False
        api._game_root = root
        api._install_path = root
        api._endpoint = "188.127.241.8:1130"
        api._nickname = "LEDERG"
        api._client_health = {}
        api._phase = "ready"
        api._status = ""
        api._error = ""
        api._progress = 0.0
        api._progress_label = ""
        api._current_file = ""
        api._process = None
        api._last_exit_code = None
        api._xpanel_stop = None
        api._repair_failure = {}
        return api

    @staticmethod
    def create_client(root: Path) -> tuple[Path, Path]:
        multiplayer = root / "multiplayer_c"
        multiplayer.mkdir(parents=True)
        game = root / "gta_sa.exe"
        game.write_bytes(b"MZ")
        runtime = multiplayer / "grand_theft_auto_san_andreas.dll"
        runtime.write_bytes(b"MZ")
        (multiplayer / "san_andreas_multiplayer.dll").write_bytes(b"MZ")
        return game, runtime

    def test_play_launches_hidden_direct_helper_and_never_opens_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            root = temp_root / "GTA Criminal Russia"
            root.mkdir()
            game, runtime = self.create_client(root)
            browser = root / "multiplayer_c" / "multiplayer_browser_cr.exe"
            browser.write_bytes(b"MZ")
            helper = temp_root / "LEDERG-DirectLaunch.exe"
            helper.write_bytes(b"MZ")
            api = self.make_api(root)
            process = Mock()
            process.pid = 1234
            process.args = ["LEDERG-DirectLaunch.exe", "--host", "188.127.241.8"]
            process.poll.return_value = None

            with (
                patch.object(LauncherApi, "set_nickname", return_value={"ok": True}),
                patch.object(LauncherApi, "_refresh_server"),
                patch("web_runtime.inspect_client_installation", return_value={
                    "healthy": True, "summary": "ok", "issues": [], "warnings": [],
                    "integrity_mode": "verified", "checked_files": 20,
                }),
                patch("web_runtime.direct_launcher_path", return_value=helper),
                patch("web_runtime.spawn_direct_launcher", return_value=process) as spawn,
                patch("web_runtime.connect_via_browser_ui") as browser_connect,
                patch.object(LauncherApi, "_start_xpanel_hotkey"),
                patch("web_runtime.threading.Thread") as thread,
            ):
                thread.return_value.start = Mock()
                result = LauncherApi.launch_game(api, "LEDERG")

            self.assertTrue(result["ok"], result)
            browser_connect.assert_not_called()
            spawn.assert_called_once()
            command = spawn.call_args.args[0]
            self.assertEqual(Path(command[0]).resolve(), helper.resolve())
            self.assertEqual(command[command.index("--game") + 1], str(game))
            self.assertEqual(
                command[command.index("--dll") + 1],
                str(root / "multiplayer_c" / "san_andreas_multiplayer.dll"),
            )
            self.assertEqual(command[command.index("--host") + 1], "188.127.241.8")
            self.assertEqual(command[command.index("--port") + 1], "1130")
            self.assertEqual(command[command.index("--nickname") + 1], "LEDERG")
            self.assertNotIn(str(browser), command)
            self.assertIs(api._process, process)
            self.assertIn("188.127.241.8:1130", api._status)

    def test_play_reports_missing_direct_helper_without_opening_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            root = temp_root / "GTA Criminal Russia"
            root.mkdir()
            self.create_client(root)
            helper = temp_root / "missing" / "LEDERG-DirectLaunch.exe"
            api = self.make_api(root)

            with (
                patch.object(LauncherApi, "set_nickname", return_value={"ok": True}),
                patch.object(LauncherApi, "_refresh_server"),
                patch("web_runtime.inspect_client_installation", return_value={
                    "healthy": True, "summary": "ok", "issues": [], "warnings": [],
                    "integrity_mode": "verified", "checked_files": 20,
                }),
                patch("web_runtime.direct_launcher_path", return_value=helper),
                patch("web_runtime.spawn_direct_launcher") as spawn,
                patch("web_runtime.connect_via_browser_ui") as browser_connect,
            ):
                result = LauncherApi.launch_game(api, "LEDERG")

            self.assertFalse(result["ok"])
            self.assertIn("LEDERG-DirectLaunch.exe", result["message"])
            spawn.assert_not_called()
            browser_connect.assert_not_called()

    def test_spawn_direct_launcher_suppresses_console_and_detaches_stdio(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process = Mock()

            with patch("web_runtime.subprocess.Popen", return_value=process) as popen:
                result = spawn_direct_launcher(["helper.exe", "--host", "127.0.0.1"], root)

            self.assertIs(result, process)
            command = popen.call_args.args[0]
            kwargs = popen.call_args.kwargs
            self.assertEqual(command, ["helper.exe", "--host", "127.0.0.1"])
            self.assertEqual(kwargs["cwd"], str(root.resolve()))
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            self.assertTrue(kwargs["close_fds"])
            if os.name == "nt":
                self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
                self.assertTrue(
                    kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
                )
                self.assertEqual(kwargs["startupinfo"].wShowWindow, subprocess.SW_HIDE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
