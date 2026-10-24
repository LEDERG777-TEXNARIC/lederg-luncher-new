from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "2.5.5"
FILES = (
    "client_updates.py",
    "radio_sync.py",
    "launcher.py",
    "web_runtime.py",
    "web/index.html",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    entries = []
    for relative in FILES:
        path = ROOT / relative
        if not path.is_file():
            raise SystemExit(f"Missing runtime source: {relative}")
        entries.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    payload = {"schema": 1, "version": VERSION, "files": entries}
    output = ROOT / "runtime-manifest.json"
    output.write_bytes((json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    print(f"Wrote {output} with {len(entries)} SHA-256-verified runtime files.")
    for item in entries:
        print(f"{item['sha256']}  {item['path']}  ({item['size']} bytes)")


if __name__ == "__main__":
    main()
