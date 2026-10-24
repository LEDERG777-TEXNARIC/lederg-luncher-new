from __future__ import annotations

import unittest

from launcher import CLIENT_PE_FILES, CLIENT_REQUIRED_FILES


class DriveClientDependencyCoverageTests(unittest.TestCase):
    def test_every_observed_runtime_module_is_required_and_architecture_checked(self):
        # These components are visible in the user's public Google Drive CR:MP folder.
        observed_runtime_modules = {
            "vorbis.dll",
            "vorbisFile.dll",
            "vorbisHooked.dll",
            "multiplayer_c/adapt.dll",
            "multiplayer_c/adaptation_helper.dll",
            "multiplayer_c/bass.dll",
            "multiplayer_c/eax.dll",
            "multiplayer_c/ogg.dll",
            "multiplayer_c/vorbis.dll",
            "multiplayer_c/vorbisFile.dll",
        }

        self.assertEqual(
            observed_runtime_modules - set(CLIENT_REQUIRED_FILES),
            set(),
            "The startup self-check is missing files visible in the real Drive client.",
        )
        self.assertEqual(
            observed_runtime_modules - set(CLIENT_PE_FILES),
            set(),
            "The PE checker must validate each required runtime binary as 32-bit x86.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
