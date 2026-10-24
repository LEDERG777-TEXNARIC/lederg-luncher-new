from __future__ import annotations

import argparse
import json
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
BINARY_SUFFIXES = {".exe", ".dll", ".pyd", ".pyc", ".amx", ".db", ".sqlite", ".sqlite3"}
ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar", ".sql", ".log", ".tmp", ".bak", ".dump"}
PRIVATE_PARTS = {
    ".git", "__pycache__", "build", "dist", "backups", "backup",
    "private", "secrets", "credentials", "database", "databases",
}


def should_include_source_path(value: str) -> bool:
    """Allow only normalized repository-relative source/document/config paths."""
    raw = str(value or "").replace("\\", "/")
    if not raw or raw.startswith("/") or "\x00" in raw:
        return False
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        return False
    if not path.parts or ":" in path.parts[0]:
        return False
    lowered_parts = {part.casefold() for part in path.parts}
    if lowered_parts & PRIVATE_PARTS:
        return False
    name = path.name.casefold()
    if name == ".env" or name.startswith(".env.") or name in {"secrets.json", "credentials.json"}:
        return False
    if name.endswith((".pem", ".p12", ".pfx", ".key", ".token")):
        return False
    if path.suffix.casefold() in BINARY_SUFFIXES | ARCHIVE_SUFFIXES:
        return False
    return True


def tracked_source_files(root: Path = ROOT) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    names = result.stdout.decode("utf-8", errors="strict").split("\x00")
    return sorted(
        name for name in names
        if name and should_include_source_path(name)
        and (root / PurePosixPath(name)).is_file()
        and not (root / PurePosixPath(name)).is_symlink()
    )


def build_source_archive(output: Path | None = None, root: Path = ROOT) -> Path:
    """Package tracked source only, plus generated launcher cover/icon if present."""
    manifest = json.loads((root / "runtime-manifest.json").read_text(encoding="utf-8"))
    version = str(manifest.get("version", "")).strip()
    if not version:
        raise ValueError("runtime-manifest.json does not contain a release version.")
    destination = output or (root / "dist" / f"LEDERG-CRMP-Source-v{version}.zip")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive_root = f"LEDERG-CRMP-Source-v{version}"
    files = tracked_source_files(root)
    if not files:
        raise RuntimeError("No tracked source files were found for the release.")

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name in files:
            bundle.write(root / PurePosixPath(name), f"{archive_root}/{name}")
        for name in ("LEDERG-CRMP-Avatar.ico", "LEDERG-CRMP-Cover.png"):
            artwork = root / name
            if artwork.is_file() and not artwork.is_symlink():
                bundle.write(artwork, f"{archive_root}/assets/{name}")
        bundle.writestr(
            f"{archive_root}/SOURCE-ARCHIVE.txt",
            f"LEDERG CR:MP Launcher source package\nVersion: {version}\n"
            f"Tracked source files: {len(files)}\n"
            "Executable binaries, databases, backups, logs, and credentials are excluded.\n",
        )
    with zipfile.ZipFile(destination, "r") as bundle:
        bad = bundle.testzip()
        if bad:
            raise RuntimeError(f"Source archive verification failed at {bad}")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Package a clean LEDERG CR:MP source release.")
    parser.add_argument("--output", type=Path, help="Optional output ZIP path.")
    args = parser.parse_args()
    archive = build_source_archive(args.output)
    print(f"Created and verified source archive: {archive} ({archive.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
