import hashlib
import json
import os
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from launcher import download_client_folder


class FolderHandler(BaseHTTPRequestHandler):
    files = {"multiplayer_c/example.dll": b"MZexample-client"}
    requests = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/client-manifest.json":
            payload = {
                "schema": 1,
                "version": "test",
                "install_root": "GTA Criminal Russia",
                "files_base_url": f"http://{self.headers['Host']}/client-files/",
                "files": [
                    {
                        "path": path,
                        "size": len(data),
                        "crc32": f"{zlib.crc32(data) & 0xffffffff:08x}",
                    }
                    for path, data in self.files.items()
                ],
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not self.path.startswith("/client-files/"):
            self.send_error(404)
            return
        path = unquote(self.path[len("/client-files/"):])
        data = self.files.get(path)
        if data is None:
            self.send_error(404)
            return
        range_header = self.headers.get("Range", "")
        start = 0
        if range_header.startswith("bytes="):
            start = int(range_header[6:].split("-", 1)[0])
        self.requests.append((path, range_header))
        if start:
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data)-1}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)


class FolderDownloadTests(unittest.TestCase):
    def setUp(self):
        FolderHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FolderHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.manifest_url = f"http://127.0.0.1:{self.server.server_port}/client-manifest.json"
        self.temp = tempfile.TemporaryDirectory()
        self.destination = Path(self.temp.name)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_downloads_files_directly_into_named_client_folder(self):
        root, count = download_client_folder(self.destination, self.manifest_url)
        target = root / "multiplayer_c" / "example.dll"
        self.assertEqual(root, (self.destination / "GTA Criminal Russia").resolve())
        self.assertEqual(count, 1)
        self.assertEqual(target.read_bytes(), b"MZexample-client")
        self.assertFalse((self.destination / ".lederg-crmp-download.zip").exists())

    def test_rejects_dot_path_in_manifest(self):
        original = FolderHandler.files
        try:
            FolderHandler.files = {".": b"unsafe"}
            with self.assertRaises(ValueError):
                download_client_folder(self.destination, self.manifest_url)
        finally:
            FolderHandler.files = original

    def test_manifest_cannot_create_parent_directories_through_symlink(self):
        original = FolderHandler.files
        install_root = self.destination / "GTA Criminal Russia"
        install_root.mkdir(parents=True)
        outside = self.destination / "outside"
        outside.mkdir()
        link = install_root / "escape"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Directory symlink is unavailable on this machine: {exc}")
        try:
            FolderHandler.files = {"escape/subdir/file.bin": b"safe"}
            with self.assertRaises(ValueError):
                download_client_folder(self.destination, self.manifest_url)
            self.assertFalse((outside / "subdir").exists(), "A manifest must not create directories outside the install root.")
        finally:
            FolderHandler.files = original

    def test_resumes_a_partial_file_with_http_range(self):
        part = (
            self.destination / "GTA Criminal Russia" / "multiplayer_c" /
            "example.dll.lederg.part"
        )
        part.parent.mkdir(parents=True)
        part.write_bytes(b"MZexample")
        root, count = download_client_folder(self.destination, self.manifest_url)
        self.assertEqual(count, 1)
        self.assertEqual((root / "multiplayer_c" / "example.dll").read_bytes(), b"MZexample-client")
        self.assertIn(("multiplayer_c/example.dll", f"bytes={len(b'MZexample')}-"), FolderHandler.requests)

    def test_rejects_path_traversal_in_manifest(self):
        original = FolderHandler.files
        try:
            FolderHandler.files = {"../outside.txt": b"unsafe"}
            with self.assertRaises(ValueError):
                download_client_folder(self.destination, self.manifest_url)
            self.assertFalse((self.destination.parent / "outside.txt").exists())
        finally:
            FolderHandler.files = original

    def test_rejects_invalid_crc_after_download(self):
        original = FolderHandler.files
        try:
            FolderHandler.files = {"multiplayer_c/example.dll": b"MZexample-client"}
            # Send a manifest that expects the wrong CRC without changing served bytes.
            original_do_get = FolderHandler.do_GET
            def bad_crc(handler):
                if handler.path == "/client-manifest.json":
                    payload = {
                        "schema": 1, "version": "test",
                        "install_root": "GTA Criminal Russia",
                        "files_base_url": f"http://{handler.headers['Host']}/client-files/",
                        "files": [{"path": "multiplayer_c/example.dll", "size": len(FolderHandler.files["multiplayer_c/example.dll"]), "crc32": "00000000"}],
                    }
                    body = json.dumps(payload).encode("utf-8")
                    handler.send_response(200)
                    handler.send_header("Content-Length", str(len(body)))
                    handler.end_headers()
                    handler.wfile.write(body)
                else:
                    original_do_get(handler)
            FolderHandler.do_GET = bad_crc
            with self.assertRaises(RuntimeError):
                download_client_folder(self.destination, self.manifest_url)
        finally:
            FolderHandler.do_GET = original_do_get
            FolderHandler.files = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
