from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from launcher import (
    CLIENT_INTEGRITY_FILENAME,
    extract_faulting_module,
    extract_faulting_module_path,
    finalize_client_integrity_manifest,
    inspect_client_installation,
    quarantine_faulting_optional_module,
    safe_extract_zip,
    select_client_crash_events,
    write_client_integrity_manifest,
)


REQUIRED_DIRECTORIES = (
    "anim", "audio", "audio/CONFIG", "audio/SFX", "audio/streams",
    "cleo", "data", "models", "movies", "text", "multiplayer_c"
)
REQUIRED_FILES = (
    "gta_sa.exe", "cleo.asi", "eax.dll", "GTAINTERFACE.dll", "LibMP.dll", "ogg.dll",
    "anim/anim.img",
    "multiplayer_c/multiplayer_browser_cr.exe",
    "multiplayer_c/grand_theft_auto_san_andreas.dll",
    "multiplayer_c/san_andreas_multiplayer.dll",
    "multiplayer_c/adapt.dll",
    "multiplayer_c/adaptation_helper.dll",
    "multiplayer_c/bass.dll",
    "multiplayer_c/eax.dll",
    "multiplayer_c/ogg.dll",
    "multiplayer_c/vorbis.dll",
    "multiplayer_c/vorbisFile.dll",
    "vorbis.dll",
    "vorbisFile.dll",
    "vorbisHooked.dll",
    "data/handling.cfg", "models/gta3.img", "models/gamemod.img", "text/american.gxt",
)
PE_FILES = (
    "gta_sa.exe", "cleo.asi", "eax.dll", "GTAINTERFACE.dll", "LibMP.dll", "ogg.dll",
    "multiplayer_c/multiplayer_browser_cr.exe",
    "multiplayer_c/grand_theft_auto_san_andreas.dll",
    "multiplayer_c/san_andreas_multiplayer.dll",
    "multiplayer_c/adapt.dll",
    "multiplayer_c/adaptation_helper.dll",
    "multiplayer_c/bass.dll",
    "multiplayer_c/eax.dll",
    "multiplayer_c/ogg.dll",
    "multiplayer_c/vorbis.dll",
    "multiplayer_c/vorbisFile.dll",
    "vorbis.dll",
    "vorbisFile.dll",
    "vorbisHooked.dll",
)


def write_fake_x86_pe(path: Path, tail: bytes = b"payload") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray(512)
    data[:2] = b"MZ"
    data[0x3C:0x40] = (0x80).to_bytes(4, "little")
    data[0x80:0x84] = b"PE\0\0"
    data[0x84:0x86] = (0x14C).to_bytes(2, "little")
    data.extend(tail)
    path.write_bytes(data)


def make_valid_client(root: Path) -> None:
    for directory in REQUIRED_DIRECTORIES:
        (root / directory).mkdir(parents=True, exist_ok=True)
    for name in PE_FILES:
        write_fake_x86_pe(root / name)
    for name in REQUIRED_FILES:
        path = root / name
        if name not in PE_FILES:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("asset:" + name).encode("utf-8"))


def build_integrity_entries(root: Path) -> list[dict]:
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == CLIENT_INTEGRITY_FILENAME:
            continue
        relative = path.relative_to(root).as_posix()
        entries.append({
            "path": relative,
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "crc32": "00000000",
            "mtime_ns": path.stat().st_mtime_ns,
        })
    return entries


class ClientSelfCheckTests(unittest.TestCase):
    def test_full_client_structure_passes_when_core_files_and_x86_pe_headers_exist(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            make_valid_client(root)

            report = inspect_client_installation(root)

        self.assertTrue(report["healthy"], report["issues"])
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["missing_files"], [])

    def test_missing_multiplayer_dll_is_reported_as_repairable_issue(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            make_valid_client(root)
            (root / "multiplayer_c" / "san_andreas_multiplayer.dll").unlink()

            report = inspect_client_installation(root)

        self.assertFalse(report["healthy"])
        self.assertTrue(any("san_andreas_multiplayer.dll" in issue for issue in report["issues"]))

    def test_invalid_executable_header_is_detected_before_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            make_valid_client(root)
            (root / "multiplayer_c" / "multiplayer_browser_cr.exe").write_bytes(b"not a PE file")

            report = inspect_client_installation(root)

        self.assertFalse(report["healthy"])
        self.assertTrue(any("multiplayer_browser_cr.exe" in issue and "бинарный файл" in issue for issue in report["issues"]))

    def test_integrity_manifest_detects_same_size_binary_corruption_after_stat_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            make_valid_client(root)
            write_client_integrity_manifest(root, build_integrity_entries(root), source="test-archive")
            dll = root / "multiplayer_c" / "san_andreas_multiplayer.dll"
            data = bytearray(dll.read_bytes())
            data[-1] ^= 0xFF
            dll.write_bytes(data)

            report = inspect_client_installation(root, full_scan=False)

        self.assertFalse(report["healthy"])
        self.assertTrue(any("san_andreas_multiplayer.dll" in issue and "sha-256" in issue.lower() for issue in report["issues"]))

    def test_archive_extraction_builds_integrity_manifest_rebased_to_actual_game_root(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            destination = temp_root / "Games"
            archive = temp_root / "client.zip"
            inventory = temp_root / "inventory.json"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("GTA Criminal Russia/data/handling.cfg", "handling-data")
                bundle.writestr("GTA Criminal Russia/multiplayer_c/san_andreas_multiplayer.dll", b"m" * 4096)
            safe_extract_zip(archive, destination, integrity_manifest_path=inventory)
            game_root = destination / "GTA Criminal Russia"
            ledger = finalize_client_integrity_manifest(game_root, destination, inventory)
            payload = json.loads(ledger.read_text(encoding="utf-8"))

        self.assertEqual(ledger.name, CLIENT_INTEGRITY_FILENAME)
        paths = {item["path"] for item in payload["files"]}
        self.assertEqual(paths, {"data/handling.cfg", "multiplayer_c/san_andreas_multiplayer.dll"})
        self.assertEqual(payload["source"], "google-drive-archive")

    def test_faulting_module_name_and_path_are_extracted_from_windows_event_text(self):
        event = (
            "Faulting application name: gta_sa.exe, "
            "Faulting module name: d3d9.dll, Exception code: 0xc0000005\\n"
            "Faulting module path: C:\\Games\\GTA Criminal Russia\\d3d9.dll"
        )
        self.assertEqual(extract_faulting_module(event), "d3d9.dll")
        self.assertEqual(
            extract_faulting_module_path(event),
            "C:\\Games\\GTA Criminal Russia\\d3d9.dll",
        )

    def test_recent_crash_events_are_filtered_to_this_install_and_access_violation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            matched = (
                "Faulting application path: "
                + str(root / "gta_sa.exe")
                + "\\nFaulting module name: d3d9.dll; Exception code: 0xc0000005"
            )
            other_install = (
                "Faulting application path: C:\\Other\\gta_sa.exe; "
                "Faulting module name: d3d9.dll; Exception code: 0xc0000005"
            )
            sibling_install = (
                "Faulting application path: "
                + str(root) + "-Old\\\\gta_sa.exe; "
                "Faulting module name: d3d9.dll; Exception code: 0xc0000005"
            )
            other_error = (
                "Faulting application path: "
                + str(root / "gta_sa.exe")
                + "\\nFaulting module name: ntdll.dll; Exception code: 0xc0000409"
            )

            found = select_client_crash_events(
                [matched, other_install, sibling_install, other_error], root
            )

        self.assertEqual(found, [matched])


    def test_only_confirmed_local_enb_d3d9_proxy_is_quarantined(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "enbseries.ini").write_text("[GLOBAL]", encoding="utf-8")
            (root / "ENBSeries_License.txt").write_text("test license", encoding="utf-8")
            proxy = root / "d3d9.dll"
            proxy.write_bytes(b"legacy ENB proxy")

            backup = quarantine_faulting_optional_module(
                root, "d3d9.dll", str(proxy)
            )

            self.assertEqual(backup.name, "d3d9.dll.lederg-disabled.bak")
            self.assertFalse(proxy.exists())
            self.assertEqual(backup.read_bytes(), b"legacy ENB proxy")
            self.assertIsNone(
                quarantine_faulting_optional_module(
                    root, "d3d9.dll", str(root / "d3d9.dll")
                )
            )

    def test_does_not_disable_local_enb_when_windows_reports_system_d3d9(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "enbseries.ini").write_text("[GLOBAL]", encoding="utf-8")
            (root / "ENBSeries_License.txt").write_text("test license", encoding="utf-8")
            proxy = root / "d3d9.dll"
            proxy.write_bytes(b"legacy ENB proxy")

            backup = quarantine_faulting_optional_module(
                root, "d3d9.dll", r"C:\\Windows\\System32\\d3d9.dll"
            )

            self.assertIsNone(backup)
            self.assertTrue(proxy.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
