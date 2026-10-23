import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from radio_sync import sync_radio_repository


def git_blob_sha(data):
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


class RadioHTTPHandler(BaseHTTPRequestHandler):
    files = {}

    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/tree":
            tree = []
            for relative, data in sorted(self.files.items()):
                tree.append({
                    "path": relative,
                    "type": "blob",
                    "mode": "100644",
                    "size": len(data),
                    "sha": git_blob_sha(data),
                })
            tree.extend([
                {"path": "README.md", "type": "blob", "mode": "100644", "size": 4, "sha": git_blob_sha(b"help")},
                {"path": "LEDERG FM/README.md", "type": "blob", "mode": "100644", "size": 4, "sha": git_blob_sha(b"help")},
            ])
            body = json.dumps({"truncated": False, "tree": tree}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not path.startswith("/raw/"):
            self.send_error(404)
            return
        relative = unquote(path[len("/raw/"):])
        data = self.files.get(relative)
        if data is None:
            self.send_error(404)
            return
        range_header = self.headers.get("Range", "")
        if range_header.startswith("bytes="):
            start = int(range_header[len("bytes="):].split("-", 1)[0])
            if start >= len(data):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.end_headers()
                return
            chunk = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data)-1}/{len(data)}")
        else:
            chunk = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(chunk)


class RadioSyncTests(unittest.TestCase):
    def setUp(self):
        RadioHTTPHandler.files = {
            "LEDERG FM/first.mp3": b"test-track-one",
            "LEDERG FM/second.ogg": b"test-track-two",
            "LEDERG RAP/rap.wav": b"rap-track",
            "LEDERG CHILL/README.md": b"not-a-track",
            "LEDERG ROCK/nested/ignored.mp3": b"nested-not-supported",
            ".github/workflows/ignored.mp3": b"hidden-not-supported",
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), RadioHTTPHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.temp = tempfile.TemporaryDirectory()
        self.game_root = Path(self.temp.name) / "GTA Criminal Russia"
        self.game_root.mkdir()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def sync(self):
        return sync_radio_repository(
            self.game_root,
            http=requests.Session(),
            tree_url=self.base + "/tree",
            raw_base_url=self.base + "/raw/",
        )

    def test_station_folders_are_created_from_root_audio_folders(self):
        result = self.sync()
        self.assertTrue(result["ok"])
        self.assertEqual(result["stations"], ["LEDERG FM", "LEDERG RAP"])
        root = self.game_root / "resound" / "radio"
        self.assertEqual((root / "LEDERG FM" / "first.mp3").read_bytes(), b"test-track-one")
        self.assertEqual((root / "LEDERG FM" / "second.ogg").read_bytes(), b"test-track-two")
        self.assertEqual((root / "LEDERG RAP" / "rap.wav").read_bytes(), b"rap-track")
        self.assertFalse((root / "LEDERG ROCK").exists())

    def test_second_sync_skips_unchanged_files(self):
        first = self.sync()
        second = self.sync()
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(first["downloaded"], 3)
        self.assertEqual(second["downloaded"], 0)
        self.assertEqual(second["skipped"], 3)

    def test_deleted_song_and_station_are_removed_on_next_sync(self):
        self.sync()
        del RadioHTTPHandler.files["LEDERG FM/first.mp3"]
        del RadioHTTPHandler.files["LEDERG FM/second.ogg"]
        self.sync()
        root = self.game_root / "resound" / "radio"
        self.assertFalse((root / "LEDERG FM" / "first.mp3").exists())
        self.assertFalse((root / "LEDERG FM" / "second.ogg").exists())
        self.assertFalse((root / "LEDERG FM").exists())
        self.assertTrue((root / "LEDERG RAP" / "rap.wav").exists())

    def test_unmanaged_local_files_are_not_deleted(self):
        self.sync()
        personal = self.game_root / "resound" / "radio" / "LEDERG FM" / "local-note.txt"
        personal.write_text("keep", encoding="utf-8")
        del RadioHTTPHandler.files["LEDERG FM/first.mp3"]
        del RadioHTTPHandler.files["LEDERG FM/second.ogg"]
        result = self.sync()
        self.assertTrue(result["ok"])
        self.assertTrue(personal.exists())
        self.assertEqual(personal.read_text(encoding="utf-8"), "keep")

    def test_invalid_path_from_remote_tree_is_rejected_without_pruning(self):
        self.sync()
        RadioHTTPHandler.files = {"../escape.mp3": b"bad"}
        with self.assertRaises(ValueError):
            self.sync()
        self.assertTrue(
            (self.game_root / "resound" / "radio" / "LEDERG RAP" / "rap.wav").exists()
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
