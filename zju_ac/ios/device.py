"""USB-only device discovery and bounded calls to separate native device tools."""

import hashlib
import json
import os
import plistlib
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..gps import Coordinates


class DeviceError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


def resource_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "ios-resources"
    return Path(__file__).resolve().parents[2] / "build/ios-resources"


@dataclass(frozen=True, repr=False)
class Device:
    serial: str
    name: str
    version: str


def validate_serial(serial: str) -> str:
    if not isinstance(serial, str) or not re.fullmatch(r"[a-fA-F0-9-]{16,64}", serial):
        raise DeviceError("设备标识无效，请重新检测 USB 设备。")
    return serial


def parse_devices(payload: object) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("DeviceList"), list):
        raise DeviceError("Apple USB 服务返回了无法识别的设备列表。")
    result = []
    for item in payload["DeviceList"][:32]:
        props = item.get("Properties", {}) if isinstance(item, dict) else {}
        if not isinstance(props, dict):
            continue
        if props.get("ConnectionType") == "USB":
            serial = validate_serial(props.get("SerialNumber"))
            if serial not in result:
                result.append(serial)
    return result


def usb_serials(cancel: threading.Event) -> list[str]:
    request = plistlib.dumps(
        {
            "MessageType": "ListDevices",
            "ClientVersionString": "ZJU-AC",
            "ProgName": "ZJU-AC",
            "kLibUSBMuxVersion": 3,
        }
    )
    deadline = time.monotonic() + 5
    try:
        with socket.create_connection(("127.0.0.1", 27015), timeout=3) as connection:
            connection.sendall(struct.pack("<IIII", 16 + len(request), 1, 8, 1) + request)

            def read(size):
                data = bytearray()
                while len(data) < size:
                    if cancel.is_set():
                        raise Cancelled()
                    if time.monotonic() >= deadline:
                        raise DeviceError("USB 设备列表读取超时。")
                    chunk = connection.recv(size - len(data))
                    if not chunk:
                        raise DeviceError("Apple USB 服务连接已断开。")
                    data.extend(chunk)
                return bytes(data)

            size, version, message, tag = struct.unpack("<IIII", read(16))
            if not 16 < size <= 1024 * 1024 or (version, message, tag) != (1, 8, 1):
                raise DeviceError("Apple USB 服务响应无效。")
            return parse_devices(plistlib.loads(read(size - 16)))
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise DeviceError("无法连接 Apple USB 服务。请安装驱动、解锁手机并确认 USB 连接。") from exc


class DeviceBackend:
    def __init__(self, directory: Path, resources: Path | None = None):
        self.directory = directory
        self.resources = resources or resource_directory()
        self._verified = False
        self._discovered = {}
        self._modern = None

    def modern_backend(self):
        if self._modern is None:
            from .modern import ModernClient

            self._modern = ModernClient(self.directory)
        return self._modern

    def verify_tools(self):
        if self._verified:
            return
        try:
            manifest = json.loads((self.resources / "manifest.json").read_text("utf-8"))
            for entry in manifest["files"]:
                name = entry["name"]
                if Path(name).name != name:
                    raise ValueError("Invalid resource name")
                if (
                    hashlib.sha256((self.resources / "win" / name).read_bytes()).hexdigest()
                    != entry["sha256"]
                ):
                    raise ValueError("Resource checksum mismatch")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DeviceError("USB 定位组件缺失或校验失败，请重新安装软件。") from exc
        self._verified = True

    def command(self, tool: str, *args: str, cancel=None, timeout=12) -> str:
        self.verify_tools()
        if tool not in {
            "ideviceinfo",
            "idevicepair",
            "idevicedevmodectl",
            "ideviceimagemounter",
            "idevicesetlocation",
        }:
            raise DeviceError("不支持的设备操作。")
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        environment = os.environ.copy()
        environment["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"
        try:
            process = subprocess.Popen(
                [str(self.resources / "win" / (tool + ".exe")), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise DeviceError("无法启动 USB 定位组件，请检查安装文件。") from exc
        deadline = time.monotonic() + timeout
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if time.monotonic() > deadline:
                    raise DeviceError("设备响应超时，请确认手机已解锁并保持 USB 连接。")
                try:
                    out, err = process.communicate(timeout=0.15)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)
        output = out.decode("utf-8", "replace")
        if process.returncode or re.search(r"(?im)^error:", output):
            text = (output + err.decode("utf-8", "replace")).lower()
            if "trust" in text or "pairingdialog" in text:
                raise DeviceError("请在 iPhone 上选择“信任此电脑”，再重试。")
            if any(word in text for word in ("passcode", "locked", "password")):
                raise DeviceError("请先解锁 iPhone，再重试。")
            raise DeviceError("USB 设备通信失败，请检查连接、信任和开发者模式。")
        return output

    @staticmethod
    def plist(output: str) -> dict:
        try:
            result = plistlib.loads(output[output.index("<?xml") :].encode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, plistlib.InvalidFileException) as exc:
            raise DeviceError("无法读取手机信息，请解锁并信任此电脑。") from exc

    def probe(self, serial: str, cancel: threading.Event) -> Device:
        validate_serial(serial)
        data = self.plist(self.command("ideviceinfo", "-u", serial, "-s", "-x", cancel=cancel))
        if data.get("UniqueDeviceID", "").lower() != serial.lower():
            raise DeviceError("设备标识已变化，请重新检测设备。")
        version = data.get("ProductVersion", "")
        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
            raise DeviceError("无法识别 iOS 系统版本。")
        name = "".join(c for c in str(data.get("DeviceName", "iPhone")) if c.isprintable())[:100]
        return Device(serial, name, version)

    def discover(self, cancel: threading.Event, refresh=False) -> list[Device]:
        serials = usb_serials(cancel)
        self._discovered = {
            serial: self._discovered[serial]
            if not refresh and serial in self._discovered
            else self.probe(serial, cancel)
            for serial in serials
        }
        return list(self._discovered.values())

    def mounted(self, device: Device, cancel: threading.Event) -> bool:
        return bool(
            self.plist(
                self.command("ideviceimagemounter", "-u", device.serial, "-l", "-x", cancel=cancel)
            ).get("ImageSignature")
        )

    def prepare(self, target: Device, cancel: threading.Event, progress) -> Device:
        device = self.probe(target.serial, cancel)
        if int(device.version.split(".")[0]) >= 17:
            self.modern_backend().prepare(device, cancel, progress)
            return device
        progress("请保持手机解锁，并在手机上确认信任此电脑。")
        self.command("idevicepair", "-u", device.serial, "pair", cancel=cancel)
        if int(device.version.split(".")[0]) >= 16:
            mode = self.command("idevicedevmodectl", "-u", device.serial, "list", cancel=cancel)
            if not re.search(r"\benabled\b", mode, re.I):
                self.command("idevicedevmodectl", "-u", device.serial, "reveal", cancel=cancel)
                raise DeviceError(
                    "请在 iPhone 设置 → 隐私与安全性中开启开发者模式，重启并确认后重试。"
                )
        if not self.mounted(device, cancel):
            versions = [device.version, ".".join(device.version.split(".")[:2])]
            candidates = [
                root / version
                for root in (self.directory / "images", self.resources / "images")
                for version in versions
            ]
            folder = next(
                (
                    p
                    for p in candidates
                    if (p / "DeveloperDiskImage.dmg").is_file()
                    and (p / "DeveloperDiskImage.dmg.signature").is_file()
                ),
                None,
            )
            if folder is None:
                raise DeviceError(
                    f"缺少 iOS {device.version} 开发者镜像，请添加到 data/ios/images 对应版本目录。"
                )
            progress("正在准备开发者镜像，请保持 USB 连接。")
            self.command(
                "ideviceimagemounter",
                "-u",
                device.serial,
                str(folder / "DeveloperDiskImage.dmg"),
                str(folder / "DeveloperDiskImage.dmg.signature"),
                cancel=cancel,
                timeout=90,
            )
            if not self.mounted(device, cancel):
                raise DeviceError("开发者镜像挂载未完成，请检查手机状态。")
        return device

    def set_location(self, device: Device, point: Coordinates, cancel: threading.Event):
        if int(device.version.split(".")[0]) >= 17:
            return self.modern_backend().set_location(device, point, cancel)
        self.command(
            "idevicesetlocation",
            "-u",
            validate_serial(device.serial),
            "--",
            f"{point.latitude:.8f}",
            f"{point.longitude:.8f}",
            cancel=cancel,
            timeout=8,
        )

    def reset(self, device: Device):
        if int(device.version.split(".")[0]) >= 17:
            return self.modern_backend().reset(device)
        self.command("idevicesetlocation", "-u", validate_serial(device.serial), "reset", timeout=8)

    def close(self):
        if self._modern is not None:
            self._modern.close()
