import hashlib
import io
import re
import requests
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

import launcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from launcher import (
    CLIENT_MANIFEST_URL,
    DEFAULT_LAUNCH_MODE,
    SERVER_IP_URL,
    ZIP_URL,
    build_launch_command,
    build_direct_launcher_command,
    build_direct_game_command,
    find_direct_game_client,
    find_injected_multiplayer_dll,
    download_archive,
    download_archive_parallel,
    download_client_folder,
    ensure_install_space,
    should_use_manifest_update,
    fetch_release_manifest,
    find_client,
    game_root_for,
    load_server_address,
    parse_server_address,
    sanitize_nickname,
    safe_extract_zip,
    ensure_legacy_directx_dependency,
    samp_registry_values,
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


class ParallelRangeTestHandler(RangeTestHandler):
    active_requests = 0
    max_active_requests = 0
    activity_lock = threading.Lock()

    def do_GET(self):
        with self.activity_lock:
            type(self).active_requests += 1
            type(self).max_active_requests = max(type(self).max_active_requests, type(self).active_requests)
        try:
            time.sleep(0.12)
            super().do_GET()
        finally:
            with self.activity_lock:
                type(self).active_requests -= 1


class LauncherTests(unittest.TestCase):
    def test_default_launch_mode_uses_the_crmp_native_cli(self):
        self.assertEqual(DEFAULT_LAUNCH_MODE, "samp-cli")

    def test_archive_fallback_uses_google_drive_virus_scan_confirmation(self):
        self.assertEqual(
            ZIP_URL,
            "https://drive.usercontent.google.com/download?id=1981J9qVnGbEjLK7lXg7L2AO1br0ATRct&export=download&confirm=t",
        )

    def test_google_drive_release_url_adds_virus_scan_confirmation(self):
        payload = {
            "schema": 1,
            "version": "test",
            "archive_url": "https://drive.usercontent.google.com/download?id=123&export=download",
        }

        release = fetch_release_manifest(
            fetcher=lambda *_args, **_kwargs: FakeResponse(payload=payload)
        )
        self.assertIn("confirm=t", release["archive_url"])
        self.assertEqual(release["archive_size"], 0)

    def test_existing_google_drive_confirmation_is_not_duplicated(self):
        payload = {
            "schema": 1,
            "version": "test",
            "archive_url": "https://drive.usercontent.google.com/download?id=123&export=download&confirm=t",
        }

        release = fetch_release_manifest(
            fetcher=lambda *_args, **_kwargs: FakeResponse(payload=payload)
        )
        self.assertEqual(release["archive_url"].count("confirm="), 1)

    def test_server_address_is_loaded_from_the_ip_repository(self):
        self.assertEqual(
            SERVER_IP_URL,
            "https://raw.githubusercontent.com/LEDERG777-TEXNARIC/lederg-ip-crmp/main/ip",
        )

    def test_nickname_is_cleaned_and_saved_value_can_be_used(self):
        self.assertEqual(sanitize_nickname("  Илья крутой!!  "), "Илья_крутой")
        self.assertEqual(sanitize_nickname("Player_Name"), "Player_Name")
        self.assertEqual(sanitize_nickname("🔥🔥"), "LEDERG")
        self.assertEqual(sanitize_nickname("A" * 40), "A" * 24)

    def test_release_manifest_cannot_reenable_browser_ui(self):
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
        self.assertEqual(release["launch_mode"], "samp-cli")

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
        self.assertEqual(
            release["archive_url"],
            CLIENT_MANIFEST_URL.rsplit("/", 1)[0] + "/downloads/GTA-Criminal-Russia.zip",
        )
        self.assertEqual(release["sha256"], digest)

    def test_legacy_browser_is_launched_without_unsupported_cli_arguments(self):
        exe = Path("multiplayer_c/multiplayer_browser_cr.exe")
        self.assertEqual(
            build_launch_command(exe, "188.127.241.8", 1130, "Player_Name", "browser-ui"),
            [str(exe)],
        )

    def test_crmp_client_does_not_pass_an_empty_rcon_password(self):
        exe = Path("multiplayer_c/multiplayer_browser_cr.exe")
        self.assertEqual(
            build_launch_command(exe, "188.127.241.8", 1130, "Player_Name"),
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

    def test_legacy_direct_play_branch_uses_the_official_client_bootstrap(self):
        source = Path(__file__).with_name("launcher.py").read_text(encoding="utf-8")
        start = source.index('if launch_mode in {"direct", "samp-cli"}', source.index("def play("))
        end = source.index('elif launch_mode == "browser-ui":', start)
        branch = source[start:end]
        self.assertIn("build_launch_command(", branch)
        self.assertNotIn("build_direct_game_command(", branch)
        self.assertNotIn("build_direct_launcher_command(", branch)
        self.assertIn("register_samp_game(", branch)
        self.assertIn("ensure_legacy_directx_dependency(", branch)

    def test_web_play_uses_crmp_bootstrap_instead_of_manual_dll_injection(self):
        source = Path(__file__).with_name("web_runtime.py").read_text(encoding="utf-8")
        start = source.index("def launch_game(self, nickname: str)")
        end = source.index("def _start_xpanel_hotkey(", start)
        branch = source[start:end]
        self.assertIn("build_launch_command(", branch)
        self.assertIn("DEFAULT_CLIENT_EXECUTABLE", branch)
        self.assertNotIn("build_direct_launcher_command(", branch)
        self.assertNotIn("CreateRemoteThread", branch)

    def test_direct_helper_registers_the_gta_executable_for_adaptation(self):
        source = Path(__file__).with_name("direct_launcher.cs").read_text(encoding="utf-8")
        self.assertIn('key.SetValue("gta_sa_exe", game, RegistryValueKind.String);', source)

    def test_samp_registry_values_register_the_actual_gta_exe_and_clean_nickname(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            game = root / "gta_sa.exe"
            game.write_bytes(b"MZ")

            values = samp_registry_values(root, " My Nick! ")

            self.assertEqual(values["gta_sa_exe"], str(game.resolve()))
            self.assertEqual(values["PlayerName"], "My_Nick")

    def test_legacy_d3dx_dependency_stays_in_game_root_and_cleans_extra_client_copy(self):
        bundle_root = Path(__file__).parent
        source = bundle_root / "dependencies" / "d3dx9_25.dll"
        self.assertTrue(source.is_file(), "the verified legacy DirectX DLL must be bundled")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            (root / "gta_sa.exe").write_bytes(b"MZ")
            multiplayer = root / "multiplayer_c"
            multiplayer.mkdir()
            extra_copy = multiplayer / "d3dx9_25.dll"
            extra_copy.write_bytes(source.read_bytes())
            local_appdata = Path(temp) / "localappdata"

            with patch.dict("os.environ", {"LOCALAPPDATA": str(local_appdata)}):
                installed = ensure_legacy_directx_dependency(root, bundle_root=bundle_root)

            self.assertEqual(installed, (root / "d3dx9_25.dll").resolve())
            self.assertTrue(installed.is_file())
            self.assertEqual(
                hashlib.sha256(installed.read_bytes()).hexdigest(),
                launcher.LEGACY_D3DX_SHA256,
            )
            self.assertFalse(extra_copy.exists(), "multiplayer_c must not contain this launcher-added DLL")
            backups = list((local_appdata / "LEDERG CRMP" / "quarantine").glob("d3dx9_25.dll.*.backup"))
            self.assertEqual(len(backups), 1, "the removed file must be preserved outside the game")
            self.assertEqual(backups[0].read_bytes(), source.read_bytes())

    def test_legacy_d3dx_dependency_replaces_a_corrupt_local_copy(self):
        bundle_root = Path(__file__).parent
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "d3dx9_25.dll"
            destination.write_bytes(b"not the DirectX DLL")

            installed = ensure_legacy_directx_dependency(root, bundle_root=bundle_root)

            self.assertEqual(
                hashlib.sha256(installed.read_bytes()).hexdigest(),
                launcher.LEGACY_D3DX_SHA256,
            )

    def test_legacy_d3dx_dependency_refuses_an_unverified_source(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle_root = Path(temp)
            (bundle_root / "dependencies").mkdir()
            (bundle_root / "dependencies" / "d3dx9_25.dll").write_bytes(b"fake DLL")
            with self.assertRaises(ValueError):
                ensure_legacy_directx_dependency(
                    bundle_root / "game", bundle_root=bundle_root
                )

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
                partial = destination / ".lederg-crmp-download.zip.part"
                partial.write_bytes(archive_bytes)
                url = f"http://127.0.0.1:{server.server_port}/downloads/client.zip"
                with self.assertRaises(RuntimeError):
                    download_archive(
                        destination,
                        url=url,
                        expected_sha256="0" * 64,
                        expected_size=len(archive_bytes),
                    )
                self.assertFalse((destination / ".lederg-crmp-download.zip").exists())
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
                partial = destination / ".lederg-crmp-download.zip.part"
                partial.write_bytes(archive_bytes[:len(archive_bytes) // 3])
                url = f"http://127.0.0.1:{server.server_port}/downloads/client.zip"
                digest = hashlib.sha256(archive_bytes).hexdigest()
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



    def test_parallel_archive_download_reassembles_and_verifies_all_ranges(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("gta_sa.exe", b"MZ" * 4096)
            bundle.writestr("multiplayer_c/multiplayer_browser_cr.exe", b"MZ" * 2048)
        archive_bytes = buffer.getvalue()
        RangeTestHandler.payload = archive_bytes
        server = ThreadingHTTPServer(("127.0.0.1", 0), RangeTestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                destination = Path(temp)
                digest = hashlib.sha256(archive_bytes).hexdigest()
                completed = download_archive_parallel(
                    destination,
                    f"http://127.0.0.1:{server.server_port}/client.zip",
                    total_size=len(archive_bytes),
                    expected_sha256=digest,
                    workers=4,
                )
                self.assertTrue(zipfile.is_zipfile(completed))
                self.assertEqual(completed.read_bytes(), archive_bytes)
                self.assertEqual(completed.name, ".lederg-crmp-download.zip")
                self.assertFalse(list(destination.glob(".lederg-crmp-download.zip.part.*")))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_download_archive_auto_uses_parallel_range_mode_for_large_supported_sources(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("gta_sa.exe", b"MZ" * 1024)
        archive_bytes = buffer.getvalue()
        RangeTestHandler.payload = archive_bytes
        server = ThreadingHTTPServer(("127.0.0.1", 0), RangeTestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original_threshold = launcher.PARALLEL_DOWNLOAD_MIN_SIZE
        launcher.PARALLEL_DOWNLOAD_MIN_SIZE = 1
        try:
            with tempfile.TemporaryDirectory() as temp:
                digest = hashlib.sha256(archive_bytes).hexdigest()
                completed = download_archive(
                    Path(temp),
                    url=f"http://127.0.0.1:{server.server_port}/client.zip",
                    expected_sha256=digest,
                    expected_size=len(archive_bytes),
                )
                self.assertEqual(completed.read_bytes(), archive_bytes)
                self.assertTrue(zipfile.is_zipfile(completed))
        finally:
            launcher.PARALLEL_DOWNLOAD_MIN_SIZE = original_threshold
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_install_space_preflight_reserves_archive_and_expanded_client(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("launcher.shutil.disk_usage", return_value=SimpleNamespace(free=599)):
                with self.assertRaises(RuntimeError):
                    ensure_install_space(Path(temp), archive_size=100, install_size=300, reserve_bytes=200)
            with patch("launcher.shutil.disk_usage", return_value=SimpleNamespace(free=600)):
                self.assertEqual(
                    ensure_install_space(Path(temp), archive_size=100, install_size=300, reserve_bytes=200),
                    600,
                )

    def test_folder_download_uses_parallel_safe_manifest_files(self):
        from launcher import zlib
        payload_bytes = {
            "gta_sa.exe": b"MZ-LEDERG-CLIENT",
            "multiplayer_c/multiplayer_browser_cr.exe": b"MZ-CRMP-CLIENT",
            "data/script.img": b"script-data",
        }
        ParallelRangeTestHandler.payload = next(iter(payload_bytes.values()))
        ParallelRangeTestHandler.max_active_requests = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), ParallelRangeTestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                destination = Path(temp)
                # This test uses a single payload for every route; expected CRCs
                # match it so we can prove the parallel install/update pipeline.
                from launcher import zlib
                blob = ParallelRangeTestHandler.payload
                manifest_payload = {
                    "schema": 1,
                    "version": "test",
                    "install_root": "GTA Criminal Russia",
                    "files_base_url": f"http://127.0.0.1:{server.server_port}/files/",
                    "files": [
                        {"path": name, "size": len(blob), "crc32": f"{zlib.crc32(blob) & 0xffffffff:08x}"}
                        for name in payload_bytes
                    ],
                }
                class LocalRouteHTTP:
                    def get(self, _url, **kwargs):
                        return requests.get(
                            f"http://127.0.0.1:{server.server_port}/files/client", **kwargs
                        )

                root, count = download_client_folder(
                    destination,
                    f"https://example.cloudpub.ru/client-manifest.json",
                    manifest_payload=manifest_payload,
                    http=LocalRouteHTTP(),
                )
                self.assertEqual(count, 3)
                self.assertEqual((root / "gta_sa.exe").read_bytes(), blob)
                self.assertEqual((root / "multiplayer_c" / "multiplayer_browser_cr.exe").read_bytes(), blob)
                self.assertEqual((root / "data" / "script.img").read_bytes(), blob)
                self.assertGreaterEqual(ParallelRangeTestHandler.max_active_requests, 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


    def test_first_install_uses_archive_and_existing_install_uses_manifest_updates(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self.assertFalse(should_use_manifest_update(base))

            root = base / "GTA Criminal Russia"
            multiplayer = root / "multiplayer_c"
            multiplayer.mkdir(parents=True)
            (root / "gta_sa.exe").write_bytes(b"MZ")
            (multiplayer / "multiplayer_browser_cr.exe").write_bytes(b"MZ")
            (multiplayer / "san_andreas_multiplayer.dll").write_bytes(b"MZ")
            self.assertTrue(should_use_manifest_update(base))

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

    def test_web_launcher_uses_official_bootstrap_cli_instead_of_manual_injection(self):
        source = Path(__file__).with_name("web_runtime.py").read_text(encoding="utf-8")
        start = source.index("def launch_game(self, nickname: str)")
        end = source.index("def _start_xpanel_hotkey(", start)
        branch = source[start:end]
        self.assertIn("command = build_launch_command(", branch)
        self.assertIn('"samp-cli"', branch)
        self.assertNotIn("build_direct_launcher_command(", branch)
        self.assertNotIn("CreateRemoteThread", branch)


if __name__ == "__main__":
    unittest.main(verbosity=2)
