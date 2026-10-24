from __future__ import annotations

import unittest
from pathlib import PurePosixPath

from tools.package_release import should_include_source_path


class ReleasePackageTests(unittest.TestCase):
    def test_keeps_launcher_code_tests_workflows_and_build_configuration(self):
        for name in (
            "launcher.py",
            "direct_launcher.cs",
            "tests/test_launcher_source_policy.py",
            ".github/workflows/build-windows.yml",
            "LEDERG CRMP.spec",
            "README.md",
        ):
            with self.subTest(name=name):
                self.assertTrue(should_include_source_path(name))

    def test_excludes_binaries_databases_secrets_backups_and_generated_output(self):
        for name in (
            "dist/LEDERG CRMP.exe",
            "build/bootstrap.exe",
            "private/.env",
            "backups/live-db.sql",
            "data/accounts.db",
            "foo/LEDERG-DirectLaunch.exe",
            "x.dll",
            "cache/__pycache__/launcher.pyc",
            "logs/build.log",
        ):
            with self.subTest(name=name):
                self.assertFalse(should_include_source_path(name))

    def test_rejects_paths_outside_the_repository(self):
        for name in ("../outside.py", "/tmp/outside.py", "C:/temp/outside.py", ""):
            with self.subTest(name=name):
                self.assertFalse(should_include_source_path(name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
