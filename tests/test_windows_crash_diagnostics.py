from __future__ import annotations

import json
import unittest

from web_runtime import parse_windows_crash_event_payload


class WindowsCrashEventParserTests(unittest.TestCase):
    def test_matches_event_to_launched_client_and_keeps_faulting_module(self):
        payload = json.dumps([
            {
                "TimeCreated": "2026-10-19T21:20:00+03:00",
                "Id": 1000,
                "ProviderName": "Application Error",
                "Message": (
                    "Faulting application name: multiplayer_browser.cr.exe, "
                    "Faulting module name: d3d9.dll, Exception code: 0xc0000005"
                ),
            },
            {
                "TimeCreated": "2026-10-19T21:20:01+03:00",
                "Id": 1000,
                "ProviderName": "Application Error",
                "Message": "Faulting application name: unrelated.exe",
            },
        ])

        events = parse_windows_crash_event_payload(
            payload, ["multiplayer_browser.cr.exe"]
        )

        self.assertEqual(len(events), 1)
        self.assertIn("Application Error", events[0])
        self.assertIn("multiplayer_browser.cr.exe", events[0])
        self.assertIn("d3d9.dll", events[0])
        self.assertIn("0xc0000005", events[0])

    def test_accepts_single_event_object_and_matches_case_insensitively(self):
        payload = json.dumps({
            "TimeCreated": "2026-10-19T21:20:00+03:00",
            "Id": 1001,
            "ProviderName": "Windows Error Reporting",
            "Message": "Problem Event Name: APPCRASH; App: GTA_SA.EXE",
        })

        events = parse_windows_crash_event_payload(payload, ["gta_sa.exe"])

        self.assertEqual(len(events), 1)
        self.assertIn("Windows Error Reporting", events[0])

    def test_returns_empty_for_invalid_or_unrelated_payload(self):
        self.assertEqual(
            parse_windows_crash_event_payload("not json", ["gta_sa.exe"]), []
        )
        self.assertEqual(
            parse_windows_crash_event_payload(
                '[{"Id": 1000, "Message": "Faulting application: other.exe"}]',
                ["gta_sa.exe"],
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
