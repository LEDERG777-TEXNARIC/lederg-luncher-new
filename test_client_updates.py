import copy
import json
import tempfile
import unittest
import zlib
from pathlib import Path
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from client_updates import (
    STATE_FILENAME,
    apply_successful_update,
    inspect_installation,
    validate_manifest,
    write_installed_state,
)


def entry(name, data):
    return {"path": name, "size": len(data), "crc32": f"{zlib.crc32(data) & 0xffffffff:08x}"}


def manifest(files, version="test"):
    return validate_manifest({
        "schema": 1,
        "version": version,
        "install_root": "GTA Criminal Russia",
        "files_base_url": "https://example.cloudpub.ru/client-files/",
        "files": [entry(name, data) for name, data in files.items()],
    })


class ClientUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "GTA Criminal Russia"
        self.root.mkdir()
        self.original = {
            "gta_sa.exe": b"gta-binary",
            "multiplayer_c/client.dll": b"client-code",
        }
        for relative, data in self.original.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.manifest = manifest(self.original)

    def tearDown(self):
        self.temp.cleanup()

    def test_matching_install_does_not_offer_update_and_records_state(self):
        report = inspect_installation(self.root, self.manifest)
        self.assertFalse(report["update_available"])
        state = json.loads((self.root / STATE_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(state["fingerprint"], self.manifest["fingerprint"])
        self.assertEqual({item["path"] for item in state["files"]}, set(self.original))

    def test_changed_remote_file_offers_update_without_touching_install(self):
        inspect_installation(self.root, self.manifest)
        altered = dict(self.original)
        altered["multiplayer_c/client.dll"] = b"client-new!"
        remote = manifest(altered, version="next")
        report = inspect_installation(self.root, remote)
        self.assertTrue(report["update_available"])
        self.assertEqual(report["changed"], ["multiplayer_c/client.dll"])
        self.assertEqual((self.root / "multiplayer_c/client.dll").read_bytes(), self.original["multiplayer_c/client.dll"])

    def test_missing_remote_file_offers_update(self):
        remote = manifest({**self.original, "models/new.txd": b"texture"})
        report = inspect_installation(self.root, remote)
        self.assertTrue(report["update_available"])
        self.assertEqual(report["missing"], ["models/new.txd"])

    def test_removed_tracked_file_offers_update_and_only_managed_file_is_pruned(self):
        old = {**self.original, "old/mod.dat": b"mod"}
        for name, data in old.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        previous = manifest(old, version="old")
        write_installed_state(self.root, previous)
        (self.root / "user-settings.ini").write_text("keep", encoding="utf-8")

        current = manifest(self.original, version="new")
        report = inspect_installation(self.root, current)
        self.assertTrue(report["update_available"])
        self.assertEqual(report["removed"], ["old/mod.dat"])
        result = apply_successful_update(self.root, current)
        self.assertEqual(result["deleted"], ["old/mod.dat"])
        self.assertFalse((self.root / "old/mod.dat").exists())
        self.assertTrue((self.root / "user-settings.ini").exists())
        self.assertEqual(
            json.loads((self.root / STATE_FILENAME).read_text(encoding="utf-8"))["fingerprint"],
            current["fingerprint"],
        )

    def test_fingerprint_depends_on_content_list_not_file_order_or_release_label(self):
        reversed_payload = manifest(dict(reversed(list(self.original.items()))), version="another-name")
        self.assertEqual(self.manifest["fingerprint"], reversed_payload["fingerprint"])

    def test_path_traversal_is_rejected(self):
        bad = {
            "schema": 1, "version": "test",
            "install_root": "GTA Criminal Russia",
            "files_base_url": "https://example.cloudpub.ru/files/",
            "files": [entry("../outside.bin", b"bad")],
        }
        with self.assertRaises(ValueError):
            validate_manifest(bad)

    def test_rejects_untrusted_plain_http_host(self):
        bad = {
            "schema": 1, "version": "test",
            "install_root": "GTA Criminal Russia",
            "files_base_url": "http://example.com/files/",
            "files": [entry("file.bin", b"x")],
        }
        with self.assertRaises(ValueError):
            validate_manifest(bad)

    def test_legacy_install_is_hashed_once_and_state_is_created(self):
        report = inspect_installation(self.root, self.manifest)
        self.assertFalse(report["update_available"])
        self.assertTrue(report["legacy_state"])
        self.assertTrue((self.root / STATE_FILENAME).is_file())
        report_again = inspect_installation(self.root, self.manifest)
        self.assertFalse(report_again["update_available"])
        self.assertFalse(report_again["legacy_state"])

    def test_external_local_change_is_detected_even_when_manifest_unchanged(self):
        inspect_installation(self.root, self.manifest)
        path = self.root / "multiplayer_c/client.dll"
        path.write_bytes(b"tampered!!!")
        report = inspect_installation(self.root, self.manifest)
        self.assertTrue(report["update_available"])
        self.assertIn("multiplayer_c/client.dll", report["changed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
