from __future__ import annotations

import tempfile
import unittest
import zlib
from pathlib import Path

from client_updates import inspect_installation, validate_manifest, write_installed_state


class ClientReleaseVersionTests(unittest.TestCase):
    def test_version_change_is_update_even_when_file_bytes_match(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = b"same client file"
            (root / "client.bin").write_bytes(data)
            crc = f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"

            def manifest(version: str) -> dict:
                return validate_manifest({
                    "schema": 1,
                    "version": version,
                    "install_root": "GTA Criminal Russia",
                    "files_base_url": "https://example.com/client/",
                    "files": [{"path": "client.bin", "size": len(data), "crc32": crc}],
                })

            old = manifest("2026.10.23.3")
            write_installed_state(root, old)
            current = manifest("2026.10.24.1")
            report = inspect_installation(root, current)

            self.assertTrue(report["update_available"])
            self.assertEqual(report["version"], "2026.10.24.1")
            self.assertEqual(report["missing"], [])
            self.assertEqual(report["changed"], [])
            self.assertEqual(report["removed"], [])

    def test_same_version_and_same_files_do_not_force_update(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = b"same client file"
            (root / "client.bin").write_bytes(data)
            crc = f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
            manifest = validate_manifest({
                "schema": 1,
                "version": "2026.10.24.1",
                "install_root": "GTA Criminal Russia",
                "files_base_url": "https://example.com/client/",
                "files": [{"path": "client.bin", "size": len(data), "crc32": crc}],
            })
            write_installed_state(root, manifest)
            report = inspect_installation(root, manifest)
            self.assertFalse(report["update_available"])


if __name__ == "__main__":
    unittest.main()
