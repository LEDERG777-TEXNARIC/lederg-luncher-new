from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import web_runtime

from web_runtime import LauncherApi


class AutomaticRepairGuardTests(unittest.TestCase):
    def make_api_stub(self, root: Path):
        api = object.__new__(LauncherApi)
        api._lock = threading.RLock()
        api._busy = False
        api._process = None
        api._game_root = root
        api._install_path = root
        api._status = ""
        api._error = ""
        api._phase = "ready"
        return api

    def test_allows_one_automatic_repair_then_blocks_repeat_within_cooldown(self):
        with tempfile.TemporaryDirectory() as temp:
            data_home = Path(temp)
            root = data_home / "GTA Criminal Russia"
            root.mkdir()
            guard = data_home / "auto-repair-state.json"
            api = self.make_api_stub(root)

            with (
                patch("web_runtime.AUTO_REPAIR_STATE_FILE", guard),
                patch("web_runtime.write_launcher_log"),
            ):
                first = api._auto_repair_allowed(root, "startup integrity")
                second = api._auto_repair_allowed(root, "same error again")

            self.assertTrue(first)
            self.assertFalse(second)
            state = json.loads(guard.read_text(encoding="utf-8"))
            self.assertEqual(state["root"], str(root.resolve()))
            self.assertEqual(state["reason"], "startup integrity")
            self.assertIn("24 часа", api._error)
            self.assertEqual(state["result"], "in_progress")

    def test_retry_cooldown_preserves_the_actual_repair_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            guard = Path(temp) / "auto-repair-state.json"
            now = 1_800_000_000.0
            actual_error = (
                "Ошибка обновления: На выбранном диске недостаточно свободного места. "
                "Нужно примерно 4.2 ГБ, доступно 1.1 ГБ."
            )
            guard.write_text(json.dumps({
                "root": str(root.resolve()),
                "attempted_at": now - 60,
                "reason": "client integrity failure",
                "result": "failed",
                "detail": actual_error,
                "phase": "download",
            }), encoding="utf-8")
            api = self.make_api_stub(root)

            with (
                patch("web_runtime.AUTO_REPAIR_STATE_FILE", guard),
                patch("web_runtime.time.time", return_value=now),
                patch("web_runtime.write_launcher_log"),
            ):
                blocked = api._auto_repair_allowed(root, "same damaged files")

            self.assertFalse(blocked)
            self.assertIn("не хватает свободного места", api._error.lower())
            self.assertIn("через 14 мин", api._error.lower())
            self.assertIn("4.2", api._repair_failure["detail"])
            self.assertIn("1.1", api._repair_failure["detail"])
            self.assertIn("диске", api._repair_failure["advice"].lower())

    def test_saved_failure_diagnosis_loads_at_launcher_startup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            guard = Path(temp) / "auto-repair-state.json"
            now = 1_800_000_000.0
            guard.write_text(json.dumps({
                "root": str(root.resolve()),
                "attempted_at": now - 60,
                "finished_at": now - 30,
                "reason": "client integrity failure",
                "result": "failed",
                "phase": "download",
                "detail": (
                    "На выбранном диске недостаточно свободного места. "
                    "Нужно примерно 4.2 ГБ, доступно 1.1 ГБ."
                ),
            }), encoding="utf-8")

            with (
                patch("web_runtime.AUTO_REPAIR_STATE_FILE", guard),
                patch("web_runtime.time.time", return_value=now),
            ):
                diagnosis = LauncherApi._load_saved_repair_failure()

            self.assertIn("свободного места", diagnosis["problem"].lower())
            self.assertIn("4.2", diagnosis["detail"])
            self.assertIn("1.1", diagnosis["detail"])
            self.assertEqual(diagnosis["retry_minutes"], 14)

    def test_failed_repair_diagnostic_classifier_identifies_google_drive_http_403(self):
        from web_runtime import diagnose_auto_repair_failure

        diagnosis = diagnose_auto_repair_failure(
            "Ошибка обновления: Не удалось подключиться к указанному архиву Google Drive. "
            "Детали: 403 Client Error: Forbidden"
        )

        self.assertIn("Google Drive", diagnosis["problem"])
        self.assertIn("403", diagnosis["detail"])
        self.assertTrue(diagnosis["advice"])

    def test_failed_repair_uses_short_retry_cooldown_instead_of_full_day(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            guard = Path(temp) / "auto-repair-state.json"
            now = 1_800_000_000.0
            guard.write_text(json.dumps({
                "root": str(root.resolve()),
                "attempted_at": now - 30,
                "reason": "archive download failed",
                "result": "failed",
            }), encoding="utf-8")
            api = self.make_api_stub(root)

            with (
                patch("web_runtime.AUTO_REPAIR_STATE_FILE", guard),
                patch("web_runtime.time.time", return_value=now),
                patch("web_runtime.write_launcher_log"),
            ):
                blocked = api._auto_repair_allowed(root, "same damaged files")
                self.assertFalse(blocked)
                self.assertIn("мин", api._error)

                guard_payload = json.loads(guard.read_text(encoding="utf-8"))
                guard_payload["attempted_at"] = now - web_runtime.AUTO_REPAIR_FAILURE_RETRY_SECONDS - 1
                guard.write_text(json.dumps(guard_payload), encoding="utf-8")

                allowed = api._auto_repair_allowed(root, "retry failed repair")

            self.assertTrue(allowed)
            self.assertEqual(json.loads(guard.read_text(encoding="utf-8"))["result"], "in_progress")

    def test_old_guard_record_without_result_can_retry_after_failure_cooldown(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            guard = Path(temp) / "auto-repair-state.json"
            now = 1_800_000_000.0
            # Version 2.4.0 recorded only these fields. If the same client
            # still lacks a trusted hash ledger, assume this prior attempt failed.
            guard.write_text(json.dumps({
                "root": str(root.resolve()),
                "attempted_at": now - web_runtime.AUTO_REPAIR_FAILURE_RETRY_SECONDS - 1,
                "reason": "legacy repair attempt",
            }), encoding="utf-8")
            api = self.make_api_stub(root)

            with (
                patch("web_runtime.AUTO_REPAIR_STATE_FILE", guard),
                patch("web_runtime.time.time", return_value=now),
                patch("web_runtime.write_launcher_log"),
            ):
                allowed = api._auto_repair_allowed(root, "legacy guard migration")

            self.assertTrue(
                allowed,
                "An old failed-repair guard should use the short retry cooldown, not the 24-hour success cooldown.",
            )
            self.assertEqual(json.loads(guard.read_text(encoding="utf-8"))["result"], "in_progress")

    def test_legacy_client_without_trusted_hashes_triggers_one_time_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            api = self.make_api_stub(root)
            api._checking_client = False
            api._client_health = {}
            api._progress = 0.0
            api._progress_label = ""
            api._current_file = ""

            healthy_legacy_report = {
                "healthy": True,
                "root": str(root),
                "issues": [],
                "warnings": ["no trusted integrity baseline"],
                "summary": "structure only",
                "integrity_mode": "structural-only",
                "checked_files": 0,
            }

            with (
                patch("web_runtime.inspect_client_installation", return_value=healthy_legacy_report),
                patch("web_runtime.collect_windows_crash_events", return_value=[]),
                patch("web_runtime.select_client_crash_events", return_value=[]),
                patch.object(LauncherApi, "_request_auto_repair", return_value=True) as request,
                patch("web_runtime.write_launcher_log"),
            ):
                LauncherApi._startup_check(api)

            request.assert_called_once()
            self.assertEqual(request.call_args.args[0], root)
            self.assertIn("SHA-256 baseline", request.call_args.args[1])

    def test_repair_is_not_started_while_game_process_is_running(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            api = self.make_api_stub(root)
            api._process = type("RunningProcess", (), {"poll": lambda self: None})()

            with (
                patch("web_runtime.AUTO_REPAIR_STATE_FILE", root / "guard.json"),
                patch("web_runtime.write_launcher_log"),
            ):
                started = api._auto_repair_allowed(root, "startup integrity")

            self.assertFalse(started)
            self.assertFalse((root / "guard.json").exists())

    def test_requests_full_restore_once_when_guard_allows_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            (root / "gta_sa.exe").write_bytes(b"fake executable")
            api = self.make_api_stub(root)
            expected = {"ok": True, "message": "started"}

            with (
                patch.object(LauncherApi, "_auto_repair_allowed", return_value=True),
                patch.object(LauncherApi, "_begin_install", return_value=expected) as begin,
                patch("web_runtime.write_launcher_log"),
            ):
                started = LauncherApi._request_auto_repair(api, root, "integrity mismatch")

            self.assertTrue(started)
            begin.assert_called_once_with(reinstall=True, auto_repair_root=root)


    def test_failed_repair_diagnosis_classifies_google_drive_http_503(self):
        from web_runtime import diagnose_auto_repair_failure

        diagnosis = diagnose_auto_repair_failure(
            "Не удалось получить архив Google Drive: HTTP 503 Service Unavailable",
            "download",
        )

        self.assertIn("серверную ошибку", diagnosis["problem"].lower())
        self.assertIn("позже", diagnosis["advice"].lower())

    def test_failed_repair_diagnosis_classifies_running_game_block(self):
        from web_runtime import diagnose_auto_repair_failure

        diagnosis = diagnose_auto_repair_failure(
            "Сначала полностью закрой CR:MP, затем запускай восстановление.",
            "schedule",
        )

        self.assertIn("всё ещё запущен", diagnosis["problem"].lower())
        self.assertIn("закрой игру", diagnosis["advice"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
