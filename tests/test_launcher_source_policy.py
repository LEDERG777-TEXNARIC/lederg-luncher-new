from __future__ import annotations

import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import launcher


class LauncherSourcePolicyTests(unittest.TestCase):
    def test_client_release_prefers_checksum_manifest_and_keeps_archive_as_fallback(self):
        payload = {
            "schema": 1,
            "version": "stale-config",
            "archive_url": "https://oddly-hopeful-rudd.cloudpub.ru/downloads/GTA-Criminal-Russia.zip",
            "archive_size": 4662986612,
            "sha256": "0edecf4be4f11f6710be5f2ac17fdbeaf64b90757509c015baa64e336876781d",
            "files_manifest_url": "https://oddly-hopeful-rudd.cloudpub.ru/client-manifest.json",
            "client_executable": "multiplayer_c/multiplayer_browser_cr.exe",
            "launch_mode": "direct",
        }
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: payload,
        )

        manifest = launcher.fetch_release_manifest(fetcher=lambda *args, **kwargs: response)

        self.assertEqual(manifest["archive_url"], launcher.DRIVE_DOWNLOAD_URL)
        self.assertEqual(manifest["files_manifest_url"], "https://oddly-hopeful-rudd.cloudpub.ru/client-manifest.json")
        self.assertEqual(manifest["archive_size"], 0)
        self.assertEqual(manifest["sha256"], "")

    def test_public_ip_file_is_currently_the_launch_endpoint_source(self):
        self.assertEqual(launcher.SERVER_IP_URL, "https://raw.githubusercontent.com/LEDERG777-TEXNARIC/lederg-ip-crmp/main/ip")
        self.assertEqual(launcher.parse_server_address("188.127.241.8:1130"), ("188.127.241.8", 1130))

    def test_server_endpoint_is_fetched_from_the_public_ip_config(self):
        captured = {}
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            text="crmp.example:8904\n",
        )

        def fetcher(url, **kwargs):
            captured["url"] = url
            return response

        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as folder:
            endpoint = launcher.load_server_address(
                Path(folder) / "server_address.txt",
                fetcher=fetcher,
            )

        self.assertEqual(captured["url"], "https://raw.githubusercontent.com/LEDERG777-TEXNARIC/lederg-ip-crmp/main/ip")
        self.assertEqual(endpoint, ("crmp.example", 8904))

    def test_only_the_exact_google_drive_file_is_an_allowed_archive_source(self):
        self.assertEqual(
            launcher._validate_client_archive_source(launcher.DRIVE_DOWNLOAD_URL),
            launcher.DRIVE_DOWNLOAD_URL,
        )
        with self.assertRaises(ValueError):
            launcher._validate_client_archive_source(
                "https://oddly-hopeful-rudd.cloudpub.ru/downloads/GTA-Criminal-Russia.zip"
            )
        with self.assertRaises(ValueError):
            launcher._validate_client_archive_source(
                "https://drive.usercontent.google.com/download?id=some-other-file&export=download"
            )

    def test_google_drive_warning_cookie_is_converted_to_confirmation_url(self):
        source = launcher.DRIVE_DOWNLOAD_URL
        confirmed = launcher._google_drive_confirmation_url(
            source,
            "<html><body>File is too large to scan</body></html>",
            {"download_warning_123": "token_123"},
        )
        query = parse_qs(urlparse(confirmed).query)

        self.assertEqual(query["id"], [launcher.DRIVE_FILE_ID])
        self.assertEqual(query["export"], ["download"])
        self.assertEqual(query["confirm"], ["token_123"])

    def test_google_drive_hidden_confirmation_token_is_supported(self):
        confirmed = launcher._google_drive_confirmation_url(
            launcher.DRIVE_DOWNLOAD_URL,
            '<html><input type="hidden" name="confirm" value="hidden_token"></html>',
            {},
        )
        self.assertEqual(
            parse_qs(urlparse(confirmed).query)["confirm"],
            ["hidden_token"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
