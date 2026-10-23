import unittest
from pathlib import Path

from web_runtime import is_game_window_process, should_trigger_xpanel_hotkey


class XPanelHotkeyTests(unittest.TestCase):
    def test_x_triggers_once_on_key_down_when_game_is_foreground(self):
        self.assertTrue(
            should_trigger_xpanel_hotkey(
                key_down=True, was_down=False, foreground_pid=321, game_pid=321
            )
        )

    def test_holding_x_does_not_repeat_the_command(self):
        self.assertFalse(
            should_trigger_xpanel_hotkey(
                key_down=True, was_down=True, foreground_pid=321, game_pid=321
            )
        )

    def test_x_key_up_does_not_trigger(self):
        self.assertFalse(
            should_trigger_xpanel_hotkey(
                key_down=False, was_down=True, foreground_pid=321, game_pid=321
            )
        )

    def test_other_foreground_window_never_receives_the_hotkey(self):
        self.assertFalse(
            should_trigger_xpanel_hotkey(
                key_down=True, was_down=False, foreground_pid=999, game_pid=321
            )
        )

    def test_missing_game_pid_never_triggers(self):
        self.assertFalse(
            should_trigger_xpanel_hotkey(
                key_down=True, was_down=False, foreground_pid=0, game_pid=None
            )
        )

    def test_child_gta_sa_process_inside_client_root_is_allowed(self):
        self.assertTrue(
            is_game_window_process(
                r"C:\LEDERG\Client\gta_sa.exe", Path(r"C:\LEDERG\Client")
            )
        )

    def test_native_runtime_inside_client_root_is_allowed(self):
        self.assertTrue(
            is_game_window_process(
                r"C:\LEDERG\Client\grand_theft_auto_san_andreas.dll",
                Path(r"C:\LEDERG\Client"),
            )
        )

    def test_same_process_name_outside_client_root_is_rejected(self):
        self.assertFalse(
            is_game_window_process(
                r"C:\OtherGame\gta_sa.exe", Path(r"C:\LEDERG\Client")
            )
        )

    def test_unrelated_executable_inside_client_root_is_rejected(self):
        self.assertFalse(
            is_game_window_process(
                r"C:\LEDERG\Client\multiplayer_browser_cr.exe",
                Path(r"C:\LEDERG\Client"),
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
