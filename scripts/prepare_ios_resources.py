"""Stage verified, separate USB executables/images without importing another app."""

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True, help="Local VRun resource directory")
    parser.add_argument("--versions", nargs="*")
    options = parser.parse_args()
    source = options.source.resolve()
    manifest = json.loads((source / "third_party/windows-manifest.json").read_text("utf-8"))
    target = ROOT / "build/ios-resources"
    target.mkdir(parents=True, exist_ok=True)
    for entry in manifest["files"]:
        name = entry["name"]
        if Path(name).name != name:
            raise SystemExit("Invalid component name")
        path = source / "libimobiledevice/win" / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise SystemExit(f"Native component checksum mismatch: {name}")
        (target / "win").mkdir(exist_ok=True)
        shutil.copy2(path, target / "win" / name)
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    versions = options.versions or [
        p.name
        for p in (source / "DeveloperDiskImage").iterdir()
        if p.is_dir() and re.fullmatch(r"\d+\.\d+(?:\.\d+)?", p.name)
    ]
    for version in versions:
        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
            raise SystemExit("Invalid image version")
        destination = target / "images" / version
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("DeveloperDiskImage.dmg", "DeveloperDiskImage.dmg.signature"):
            shutil.copy2(source / "DeveloperDiskImage" / version / name, destination / name)
    for name in ("LICENSES", "third_party/sources"):
        shutil.copytree(source / name, target / name, dirs_exist_ok=True)
    shutil.copy2(source / "THIRD_PARTY_NOTICES.md", target / "REFERENCE_NOTICES.md")
    if (ROOT / "location.md").is_file():
        shutil.copy2(ROOT / "location.md", target / "location.md")
    print(f"USB resources staged: {target}")


if __name__ == "__main__":
    main()
