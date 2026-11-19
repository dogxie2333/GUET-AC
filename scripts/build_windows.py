"""Build the Windows application and its Inno Setup installer."""

import importlib.metadata as metadata
import os
import shutil
import struct
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def collect_ios17_notices(target: Path):
    """Keep upstream licenses and Python component sources with the local bundle."""
    for name in (
        "pymobiledevice3",
        "pmd-pytcp",
        "pmd-net-addr",
        "pmd-net-proto",
        "qh3",
        "cryptography",
        "pytun-pmd3",
        "sslpsk-pmd3",
        "developer_disk_image",
        "pywin32",
    ):
        dist = metadata.distribution(name)
        directory = target / "licenses" / name
        for item in dist.files or []:
            if ".." in item.parts or item.is_absolute():
                continue
            if any(
                part.lower().startswith(("license", "copying", "notice")) for part in item.parts
            ):
                source = Path(dist.locate_file(item))
                if source.is_file():
                    destination = directory / Path(*item.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
        if name in {"pymobiledevice3", "pmd-pytcp"}:
            directory.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(
                directory / "upstream-python-sources.zip", "w", zipfile.ZIP_DEFLATED
            ) as archive:
                for item in dist.files or []:
                    if ".." not in item.parts and not item.is_absolute() and item.suffix == ".py":
                        source = Path(dist.locate_file(item))
                        if source.is_file():
                            archive.write(source, str(item))


def find_compiler() -> Path:
    candidates = [
        os.environ.get("INNO_SETUP_COMPILER"),
        shutil.which("ISCC.exe"),
        ROOT / "build/tools/inno/ISCC.exe",
    ]
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        if directory := os.environ.get(variable):
            candidates.extend(Path(directory) / f"Inno Setup {v}/ISCC.exe" for v in (6, 7))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise SystemExit("请安装 Inno Setup 6.7+，或设置 INNO_SETUP_COMPILER 为 ISCC.exe 的路径。")


def make_icon(target: Path) -> None:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(str(ROOT / "zju_ac/desktop/assets/mark.svg"))
    images = []
    for size in (16, 32, 48, 64, 128, 256):
        image = QImage(size, size, QImage.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, "PNG")
        images.append((size, bytes(data)))
    offset = 6 + 16 * len(images)
    header = struct.pack("<HHH", 0, 1, len(images))
    for size, data in images:
        header += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    target.write_bytes(header + b"".join(data for _, data in images))


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("请在 Windows 的 Python 3.12 环境中运行。")
    compiler = find_compiler()
    build = ROOT / "build/windows"
    build.mkdir(parents=True, exist_ok=True)
    usb_resources = ROOT / "build/ios-resources"
    if not (usb_resources / "manifest.json").is_file():
        raise SystemExit("请先运行 scripts/prepare_ios_resources.py --source <本地 VRun 目录>。")
    if (ROOT / "location.md").is_file():
        shutil.copy2(ROOT / "location.md", usb_resources / "location.md")
    collect_ios17_notices(usb_resources)
    icon = build / "app.ico"
    make_icon(icon)
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    app_version = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    version = tuple(int(part) for part in app_version.split(".")) + (0,)
    info = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=version,
            prodvers=version,
            mask=0x3F,
            flags=0,
            OS=0x40004,
            fileType=1,
            subtype=0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("FileDescription", "ZJU-AC"),
                            StringStruct("ProductName", "ZJU-AC"),
                            StringStruct("FileVersion", app_version),
                            StringStruct("ProductVersion", app_version),
                            StringStruct("OriginalFilename", "ZJU-AC.exe"),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )
    version_file = build / "version.txt"
    version_file.write_text(str(info), encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--windowed",
        "--noupx",
        "--name",
        "ZJU-AC",
        "--icon",
        str(icon),
        "--version-file",
        str(version_file),
        "--distpath",
        str(build / "payload"),
        "--workpath",
        str(build / "work"),
        "--specpath",
        str(build),
        "--paths",
        str(ROOT),
        "--hidden-import",
        "PySide6.QtQuickControls2",
        "--hidden-import",
        "PySide6.QtSvg",
        "--exclude-module",
        "PySide6.QtWebEngineCore",
        "--exclude-module",
        "PySide6.QtWebEngineWidgets",
        "--exclude-module",
        "pytest",
        "--exclude-module",
        "IPython",
        "--exclude-module",
        "av",
        "--hidden-import",
        "win32security",
        "--hidden-import",
        "win32timezone",
        "--collect-data",
        "pymobiledevice3",
        "--recursive-copy-metadata",
        "pymobiledevice3",
        "--copy-metadata",
        "developer_disk_image",
        "--collect-submodules",
        "pmd_pytcp",
        "--collect-data",
        "pytun_pmd3",
        "--collect-binaries",
        "pytun_pmd3",
        "--collect-binaries",
        "sslpsk_pmd3",
    ]
    for source, destination in (
        (ROOT / "config.toml", "."),
        (ROOT / "zju_ac/desktop/qml", "zju_ac/desktop/qml"),
        (ROOT / "zju_ac/desktop/assets", "zju_ac/desktop/assets"),
        (ROOT / "build/ios-resources", "ios-resources"),
    ):
        command += ["--add-data", f"{source}{os.pathsep}{destination}"]
    command.append(str(ROOT / "scripts/desktop_entry.py"))
    environment = os.environ.copy()
    environment["PYINSTALLER_CONFIG_DIR"] = str(build / "cache")
    subprocess.run(command, cwd=ROOT, check=True, env=environment)
    subprocess.run(
        [
            str(compiler),
            f"/DAppVersion={app_version}",
            f"/DPayloadDir={build / 'payload/ZJU-AC'}",
            f"/DOutputDir={ROOT / 'dist'}",
            f"/DIconFile={icon}",
            str(ROOT / "scripts/windows_installer.iss"),
        ],
        cwd=ROOT,
        check=True,
    )
    print(f"\n安装包：{ROOT / 'dist' / f'ZJU-AC-Setup-{app_version}.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
