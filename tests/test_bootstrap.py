from __future__ import annotations

import hashlib
import unittest

import bootstrap


class BootstrapManifestTests(unittest.TestCase):
    def valid_payload(self):
        return {
            "schema": 1,
            "version": "test",
            "files": [
                {"path": name, "size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
                for name in sorted(bootstrap.EXPECTED_FILES)
            ],
        }

    def test_valid_manifest_is_normalized_and_contains_only_allowed_files(self):
        normalized = bootstrap._safe_manifest(self.valid_payload())
        self.assertEqual(normalized["version"], "test")
        self.assertEqual({item["path"] for item in normalized["files"]}, bootstrap.EXPECTED_FILES)

    def test_unknown_path_is_rejected(self):
        payload = self.valid_payload()
        payload["files"][0]["path"] = "../outside.py"
        with self.assertRaises(ValueError):
            bootstrap._safe_manifest(payload)

    def test_partial_manifest_is_rejected(self):
        payload = self.valid_payload()
        payload["files"].pop()
        with self.assertRaises(ValueError):
            bootstrap._safe_manifest(payload)

    def test_invalid_sha256_is_rejected(self):
        payload = self.valid_payload()
        payload["files"][0]["sha256"] = "not-a-hash"
        with self.assertRaises(ValueError):
            bootstrap._safe_manifest(payload)

    def test_oversized_runtime_entry_is_rejected(self):
        payload = self.valid_payload()
        payload["files"][0]["size"] = 20 * 1024 * 1024 + 1
        with self.assertRaises(ValueError):
            bootstrap._safe_manifest(payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
