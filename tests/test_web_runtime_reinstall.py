from __future__ import annotations

import unittest
from unittest.mock import patch

from web_runtime import LauncherApi


class LauncherReinstallApiTests(unittest.TestCase):
    def test_partial_client_is_exposed_for_manual_recovery(self):
        import tempfile
        import threading
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp:
            selected = Path(temp)
            partial = selected / "GTA Criminal Russia"
            (partial / "multiplayer_c").mkdir(parents=True)

            api = object.__new__(LauncherApi)
            api._lock = threading.RLock()
            api._game_root = None
            api._install_path = selected
            api._busy = False
            api._checking_client = False
            api._phase = "error"
            api._status = "client files are incomplete"
            api._progress = 0.0
            api._progress_label = "AUTOCHECK FAILED"
            api._current_file = ""
            api._error = api._status
            api._endpoint = "188.127.241.8:1130"
            api._nickname = "LEDERG"
            api._server_online = False
            api._server_ping_ms = None
            api._server_method = "none"
            api._server_detail = "no reply"
            api._server_checked_at = ""
            api._last_exit_code = None
            api._client_health = {
                "healthy": False,
                "summary": "incomplete client",
                "issues": ["missing gta_sa.exe"],
                "warnings": [],
                "integrity_mode": "structural-only",
            }

            with (
                patch("web_runtime.find_direct_game_client", return_value=None),
                patch("web_runtime.find_client", return_value=None),
            ):
                state = api.get_state()

            self.assertTrue(state["client_found"])
            self.assertFalse(state["client_ready"])

    def test_reinstall_entry_point_requests_repair_mode(self):
        api = object.__new__(LauncherApi)
        expected = {"ok": True, "message": "repair started"}

        with patch.object(LauncherApi, "_begin_install", return_value=expected) as begin:
            result = api.reinstall_client()

        begin.assert_called_once_with(reinstall=True)
        self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
