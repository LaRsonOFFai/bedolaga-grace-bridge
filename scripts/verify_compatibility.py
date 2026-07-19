#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

UPSTREAM = "https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot.git"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True)


def verify(root: Path) -> None:
    manifest = json.loads((root / "patches/bedolaga/compatibility.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != 1:
        raise SystemExit("Unsupported compatibility manifest")
    with tempfile.TemporaryDirectory(prefix="grace-compat-") as temporary:
        for record in manifest["supported"]:
            checkout = Path(temporary) / record["version"]
            command("git", "clone", "--filter=blob:none", UPSTREAM, str(checkout))
            command("git", "switch", "--detach", record["commit"], cwd=checkout)
            patch = root / record["patch"]
            if sha256(patch) != record["patchSha256"]:
                raise SystemExit(f"Patch checksum mismatch: {patch}")
            for relative, expected in record["files"].items():
                actual = sha256(checkout / relative)
                if actual != expected:
                    raise SystemExit(f"Source checksum mismatch: {record['version']} {relative}")
            command("git", "apply", "--check", str(patch), cwd=checkout)
            command("git", "apply", str(patch), cwd=checkout)
            command("python", "-m", "compileall", "-q", "app", cwd=checkout)
            print(f"OK {record['version']} {record['commit'][:12]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    verify(args.root.resolve())


if __name__ == "__main__":
    main()
