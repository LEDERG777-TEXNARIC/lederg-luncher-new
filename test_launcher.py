import hashlib
import io
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from launcher import (
    CLIENT_MANIFEST_URL,
    DEFAULT_LAUNCH_MODE,
    SERVER_IP_URL,
    build_launch_command,
    build_direct_launcher_command,
    build_direct_game_command,
    find_direct_game_client,
    find_injected_multiplayer_dll,
    download_archive,
    fetch_release_manifest,
    find_client,
    game_root_for,
    load_server_address,
    parse_server_address,
    sanitize_nickname,
    safe_extract_zip,
)


class FakeResponse:
    def __init__(self, text="", payload=None):
        self.text = text
        self._payload = payload
        self.headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class RangeTestHandler(BaseHTTPRequestHandler):
    payload = b""

    def _headers(self, status, start=0, end=None, partial=False):
        end = len(self.payload) - 1 if end is None else end
        self.send_response(status)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"test-client-archive"')
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.payload)}")
        self.end_headers()

    def do_HEAD(self):
        self._headers(200, 0, len(self.payload) - 1)

    def do_GET(self):
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else len(self.payload) - 1
            if start >= len(self.payload) or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(self.payload)}")
                self.end_headers()
                return
            end = min(end, len(self.payload) - 1)
            self._headers(206, start, end, partial=True)
            body = self.payload[start:end + 1]
        else:
            self._headers(200, 0, len(self.payload) - 1)
            body = self.payload
        try:
            for offset in range(0, len(body), 16384):
                self.wfile.write(body[offset:offset + 16384])
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


class LauncherTests(unittest.TestCase):
    def test_default_launch_mode_uses_native_direct_connect(self):
        self.assertEqual(DEFAULT_LAUNCH_MODE, "direct")

    def test_nickname_is_cleaned_and_saved_value_can_be_used(self):
        self.assertEqual(sanitize_nickname("  Илья крутой!!  "), "Илья_крутой")
        self.assertEqual(sanitize_nickname("Player_Name"), "Player_Name")
        self.assertEqual(sanitize_nickname("🔥🔥"), "LEDERG")
        self.assertEqual(sanitize_nickname("A" * 40), "A" * 24)

    def test_release_manifest_cannot_switch_native_launch_back_to_browser_ui(self):
        payload = {
            "schema": 1,
            "version": "test",
            "archive_url": "client.zip",
            "launch_mode": "browser-ui",
        }

        def fake_get(url, timeout):
            self.assertEqual(url, CLIENT_MANIFEST_URL)
            return FakeResponse(payload=payload)

        release = fetch_release_manifest(fetcher=fake_get)
        self.assertEqual(release["launch_mode"], "direct")

    def test_public_ip_source_is_the_configurable_github_file(self):
        self.assertEqual(
            SERVER_IP_URL,
            "https://raw.githubusercontent.com/LEDERG777-TEXNARIC/lederg-ip-crmp/main/ip",
        )

    def test_parse_current_server_endpoint(self):
        self.assertEqual(parse_server_address("# LEDERG endpoint\n188.127.241.8:1130\n"), ("188.127.241.8", 1130))

    def test_parse_server_endpoint_rejects_invalid_port_and_ambiguous_file(self):
        for value in ("192.0.2.1:0", "192.0.2.1:65536", "192.0.2.1", "1.1.1.1:123\n2.2.2.2:456"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_server_address(value)

    def test_server_address_is_fetched_and_cached(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "server.txt"
            calls = []
            def fake_get(url, timeout):
                calls.append((url, timeout))
                return FakeResponse(text="188.127.241.8:1130\n")
            self.assertEqual(load_server_address(cache, fetcher=fake_get), ("188.127.241.8", 1130))
            self.assertEqual(cache.read_text(encoding="utf-8"), "188.127.241.8:1130")
            self.assertEqual(calls[0][0], SERVER_IP_URL)

    def test_server_address_uses_last_good_cache_if_github_temporarily_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "server.txt"
            cache.write_text("188.127.241.8:1130", encoding="utf-8")
            def broken_get(*_args, **_kwargs):
                raise OSError("offline")
            self.assertEqual(load_server_address(cache, fetcher=broken_get), ("188.127.241.8", 1130))

    def test_release_manifest_resolves_relative_archive_and_validates_hash(self):
        digest = "a" * 64
        payload = {
            "schema": 1,
            "version": "1.0.1",
            "archive_url": "/downloads/GTA-Criminal-Russia.zip",
            "archive_size": 123456,
            "sha256": digest,
            "install_root": ".",
            "client_executable": "multiplayer_c/multiplayer_browser_cr.exe",
            "launch_mode": "samp-cli",
        }
        def fake_get(url, timeout):
            self.assertEqual(url, CLIENT_MANIFEST_URL)
            return FakeResponse(payload=payload)
        release = fetch_release_manifest(fetcher=fake_get)
        self.assertEqual(release["version"], "1.0.1")
        from launcher import CLIENT_FILES_MANIFEST_URL

        from launcher import DRIVE_DOWNLOAD_URL

        self.assertEqual(release["archive_url"], DRIVE_DOWNLOAD_URL)
        self.assertEqual(release["sha256"], "")
        self.assertEqual(release["archive_size"], 0)
        self.assertEqual(release["files_manifest_url"], CLIENT_FILES_MANIFEST_URL)

    def test_legacy_browser_is_launched_without_unsupported_cli_arguments(self):
        exe = Path("multiplayer_c/multiplayer_browser_cr.exe")
        self.assertEqual(
            build_launch_command(exe, "188.127.241.8", 1130, "Player_Name", "browser-ui"),
            [str(exe)],
        )

    def test_crmp_client_does_not_pass_an_empty_rcon_password(self):
        exe = Path("multiplayer_c/multiplayer_browser_cr.exe")
        self.assertEqual(
            build_launch_command(exe, "188.127.241.8", 1130, "Player_Name", "samp-cli"),
            [str(exe), "188.127.241.8:1130", "-n", "Player_Name"],
        )

    def test_direct_helper_uses_the_explicit_executable_passed_by_launcher(self):
        source = Path(__file__).with_name("direct_launcher.cs").read_text(encoding="utf-8")
        self.assertIn('string game = Path.GetFullPath(options["--game"]);', source)

    def test_direct_helper_passes_native_connect_arguments(self):
        source = Path(__file__).with_name("direct_launcher.cs").read_text(encoding="utf-8")
        self.assertIn('" -c -n " + Quote(nickname)', source)
        self.assertIn('" -h " + host + " -p " + port.ToString(CultureInfo.InvariantCulture)', source)
        self.assertNotIn('Quote(game) + " " + host + ":"', source)

    def test_direct_launch_targets_game_and_multiplayer_dll_not_the_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            multiplayer = root / "multiplayer_c"
            multiplayer.mkdir(parents=True)
            game = root / "gta_sa.exe"
            dll = multiplayer / "san_andreas_multiplayer.dll"
            game.write_bytes(b"MZ")
            dll.write_bytes(b"MZ")
            client_game = multiplayer / "grand_theft_auto_san_andreas.dll"
            client_game.write_bytes(b"MZ")
            helper = Path(temp) / "LEDERG-DirectLaunch.exe"

            command = build_direct_launcher_command(
                helper, root, "188.127.241.8", 1130, " My Nick! "
            )

            self.assertEqual(command[0], str(helper))
            self.assertEqual(command[command.index("--game") + 1], str(game))
            self.assertEqual(command[command.index("--dll") + 1], str(dll))
            self.assertEqual(command[command.index("--host") + 1], "188.127.241.8")
            self.assertEqual(command[command.index("--port") + 1], "1130")
            self.assertEqual(command[command.index("--nickname") + 1], "My_Nick")
            self.assertNotIn(str(multiplayer / "multiplayer_browser_cr.exe"), command)

    def test_find_client_honors_manifest_relative_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            preferred = root / "custom" / "client.exe"
            preferred.parent.mkdir()
            preferred.write_bytes(b"MZ")
            self.assertEqual(find_client(root, "custom/client.exe"), preferred)

    def test_game_root_uses_gta_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "gta_sa.exe").write_bytes(b"MZ")
            exe = root / "multiplayer_c" / "multiplayer_browser_cr.exe"
            exe.parent.mkdir()
            exe.write_bytes(b"MZ")
            self.assertEqual(game_root_for(exe), root)

    def test_zip_extracts_inside_selected_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "client.zip"
            destination = root / "PickedFolder"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("gta_sa.exe", b"MZ")
                bundle.writestr("multiplayer_c/multiplayer_browser_cr.exe", b"MZ")
            count = safe_extract_zip(archive, destination)
            self.assertEqual(count, 2)
            self.assertEqual(
                (destination / "multiplayer_c" / "multiplayer_browser_cr.exe").read_bytes(),
                b"MZ",
            )

    def test_full_partial_archive_with_wrong_hash_is_not_accepted_after_416(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("gta_sa.exe", b"MZ" * 128)
        archive_bytes = buffer.getvalue()
        RangeTestHandler.payload = archive_bytes
        server = ThreadingHTTPServer(("127.0.0.1", 0), RangeTestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                destination = Path(temp)
                partial = destination / ".lederg-crmp-google-drive.zip.part"
                partial.write_bytes(archive_bytes)
                url = f"http://127.0.0.1:{server.server_port}/downloads/client.zip"
                with patch("launcher._validate_client_archive_source", side_effect=lambda source: source):
                    with self.assertRaises(RuntimeError):
                        download_archive(
                            destination,
                            url=url,
                            expected_sha256="0" * 64,
                            expected_size=len(archive_bytes),
                        )
                self.assertFalse((destination / ".lederg-crmp-google-drive.zip").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_zip_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "bad.zip"
            destination = root / "PickedFolder"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../outside.txt", "bad")
            with self.assertRaises(ValueError):
                safe_extract_zip(archive, destination)
            self.assertFalse((root / "outside.txt").exists())

    def test_download_resumes_partial_archive_from_http_range_and_verifies_sha256(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("gta_sa.exe", b"MZ" * 8192)
            bundle.writestr("multiplayer_c/multiplayer_browser_cr.exe", b"MZ" * 4096)
        archive_bytes = buffer.getvalue()
        RangeTestHandler.payload = archive_bytes
        server = ThreadingHTTPServer(("127.0.0.1", 0), RangeTestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                destination = Path(temp)
                partial = destination / ".lederg-crmp-google-drive.zip.part"
                partial.write_bytes(archive_bytes[:len(archive_bytes) // 3])
                url = f"http://127.0.0.1:{server.server_port}/downloads/client.zip"
                digest = hashlib.sha256(archive_bytes).hexdigest()
                with patch("launcher._validate_client_archive_source", side_effect=lambda source: source):
                    completed = download_archive(
                        destination, url=url, expected_sha256=digest, expected_size=len(archive_bytes)
                    )
                self.assertTrue(zipfile.is_zipfile(completed))
                self.assertEqual(completed.read_bytes(), archive_bytes)
                self.assertFalse(partial.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


    def test_direct_crmp_launch_uses_native_client_and_supported_arguments(self):
        from launcher import build_direct_game_command
        executable = Path(r"C:\Game\multiplayer_c\grand_theft_auto_san_andreas.dll")
        command = build_direct_game_command(executable, "188.127.241.8", 1130, " LEDERG !! ")
        self.assertEqual(command, [
            str(executable), "-c", "-n", "LEDERG", "-h", "188.127.241.8", "-p", "1130"
        ])

    def test_find_direct_client_prefers_the_native_crmp_runtime(self):
        from launcher import find_direct_game_client
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "multiplayer_c" / "grand_theft_auto_san_andreas.dll"
            runtime.parent.mkdir(parents=True)
            runtime.write_bytes(b"MZ")
            (runtime.parent / "multiplayer_browser_cr.exe").write_bytes(b"MZ")
            self.assertEqual(find_direct_game_client(root), runtime)

    def test_direct_crmp_command_rejects_bad_endpoint(self):
        from launcher import build_direct_game_command
        with self.assertRaises(ValueError):
            build_direct_game_command(Path("client.dll"), "bad host", 1130, "LEDERG")
        with self.assertRaises(ValueError):
            build_direct_game_command(Path("client.dll"), "188.127.241.8", 70000, "LEDERG")

    def test_injected_multiplayer_dll_is_found_in_client_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            multiplayer = root / "multiplayer_c"
            multiplayer.mkdir()
            dll = multiplayer / "san_andreas_multiplayer.dll"
            dll.write_bytes(b"MZ")
            self.assertEqual(find_injected_multiplayer_dll(root), dll)

    def test_web_launcher_uses_hidden_direct_helper_and_never_launches_browser(self):
        source = Path(__file__).with_name("web_runtime.py").read_text(encoding="utf-8")
        self.assertIn("helper = direct_launcher_path().resolve()", source)
        self.assertIn("command = build_direct_launcher_command(", source)
        self.assertIn("process = spawn_direct_launcher(command, root)", source)
        self.assertIn("self._refresh_server()", source)
        self.assertNotIn("connect_via_browser_ui(browser, host, port, self._nickname)", source)
        self.assertNotIn("find_client(root, DEFAULT_CLIENT_EXECUTABLE)", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
