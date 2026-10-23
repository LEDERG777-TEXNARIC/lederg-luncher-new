from pathlib import Path
import unittest

WEB_PAGE = Path(__file__).resolve().parents[1] / "web" / "index.html"


class PublicWebLauncherTests(unittest.TestCase):
    def test_launcher_has_new_command_center_visual_language_and_transfer_status(self):
        page = WEB_PAGE.read_text(encoding="utf-8")
        self.assertIn("LEDERG COMMAND", page)
        self.assertIn("command-shell", page)
        self.assertIn("download-engine", page)
        self.assertIn("8-WAY RANGE", page)
        self.assertIn("transferSpeed", page)
        self.assertIn("transferEta", page)

    def test_browser_fallback_offers_native_launcher_download(self):
        page = WEB_PAGE.read_text(encoding="utf-8")
        self.assertIn("WEB_DOWNLOAD_URL", page)
        self.assertIn("OPEN_BROWSER_UNSUPPORTED", page)
        self.assertIn("window.pywebview", page)

    def test_browser_fallback_explains_native_launch_boundary(self):
        page = WEB_PAGE.read_text(encoding="utf-8")
        self.assertIn("addEventListener(\"click\"", page)


if __name__ == "__main__":
    unittest.main()
