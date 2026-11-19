"""Persistent iOS 17+ USB/DVT session isolated from the Qt/account processes."""

import asyncio
import logging
import multiprocessing
import os
import time
from contextlib import AsyncExitStack, suppress
from pathlib import Path

from ..gps import Coordinates
from .device import Cancelled, DeviceError, validate_serial


def load_dependencies(directory: Path):
    # Only the isolated helper imports this large dependency graph. Its private
    # pair records and personalized images belong to the application data folder.
    if os.name == "nt":
        # Also works with pip --target, whose .pth bootstrap is not automatic.
        import pywintypes  # noqa: F401

    import pymobiledevice3.common as common

    common._HOMEFOLDER = directory
    from pymobiledevice3.remote import tunnel_service
    from pymobiledevice3.remote.common import TunnelProtocol
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane, UserspaceRsdTunnel
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
    from pymobiledevice3.services.mobile_image_mounter import (
        MobileImageMounterService,
        auto_mount,
        image_type_for_device,
    )

    return {
        "tunnels": tunnel_service,
        "protocol": TunnelProtocol,
        "rsd": RemoteServiceDiscoveryService,
        "dial": UserspaceDialPlane,
        "usb": UserspaceRsdTunnel,
        "dvt": DvtProvider,
        "location": LocationSimulation,
        "mounter": MobileImageMounterService,
        "mount": auto_mount,
        "image_type": image_type_for_device,
    }


class ModernSession:
    def __init__(self, dependencies):
        self.api = dependencies
        self.stack = None
        self.location = None
        self.serial = None
        self.possibly_modified = False

    async def prepare(self, serial: str, version: str):
        validate_serial(serial)
        if self.stack is not None:
            if self.serial != serial:
                raise DeviceError("请先恢复当前设备，再连接另一台手机。")
            return
        stack = AsyncExitStack()
        try:
            parts = tuple(int(part) for part in version.split(".")[:2])
            if parts >= (17, 4):
                rsd = await stack.enter_async_context(
                    self.api["usb"](serial=serial, autopair=True, remotepairing_fallback=False)
                )
            else:
                # Older iOS exposes RSD on the USB NCM interface. Keep its QUIC
                # transport, but use the in-process stack rather than a system TUN.
                tunnels = self.api["tunnels"]
                tunnels.USE_USERSPACE_TUNNEL = True
                providers = await tunnels.get_core_device_tunnel_services(udid=serial)
                if not providers:
                    raise DeviceError(
                        "未发现 iOS 17.0–17.3 的 USB 网络接口。"
                        "请检查 Apple 驱动、IPv6 和开发者模式。"
                    )
                provider = None
                for candidate in providers:
                    if candidate.rsd.udid.lower() == serial.lower() and provider is None:
                        provider = candidate
                    else:
                        await candidate.close()
                if provider is None:
                    raise DeviceError("未找到所选设备的 USB 开发者服务。")
                stack.push_async_callback(provider.close)
                result = await stack.enter_async_context(provider.start_quic_tunnel())
                result.client.tun.set_peer(result.address)
                dial = await stack.enter_async_context(
                    self.api["dial"](result.client.tun, result.address)
                )
                rsd = self.api["rsd"](
                    (result.address, result.port),
                    open_connection=dial.dial,
                    auxiliary_metadata=result.auxiliary_metadata,
                )
                stack.push_async_callback(rsd.close)
                await rsd.connect()
            if rsd.udid.lower() != serial.lower():
                raise DeviceError("隧道连接的设备标识不匹配，已停止定位操作。")
            async with self.api["mounter"](rsd) as mounter:
                mounted = await mounter.is_image_mounted(self.api["image_type"](rsd))
            if not mounted:
                await self.api["mount"](rsd)
            dvt = await stack.enter_async_context(self.api["dvt"](rsd))
            self.location = await stack.enter_async_context(self.api["location"](dvt))
        except BaseException:
            await stack.aclose()
            self.api["tunnels"].USE_USERSPACE_TUNNEL = False
            raise
        self.stack, self.serial = stack, serial

    async def set(self, longitude, latitude):
        point = Coordinates(latitude, longitude)
        if self.location is None:
            raise DeviceError("iOS 17 定位通道尚未准备完成。")
        self.possibly_modified = True
        await self.location.set(point.latitude, point.longitude)

    async def clear(self):
        if self.location is not None:
            await self.location.clear()
            self.possibly_modified = False

    async def close(self):
        try:
            if self.possibly_modified:
                await asyncio.wait_for(self.clear(), timeout=8)
        finally:
            if self.stack is not None:
                await self.stack.aclose()
            self.stack = self.location = self.serial = None
            self.api["tunnels"].USE_USERSPACE_TUNNEL = False


def friendly_failure(error: Exception) -> str:
    if isinstance(error, DeviceError):
        return str(error)
    name = type(error).__name__.lower()
    if "developer" in name:
        return "请在手机上开启开发者模式，重启并确认后重试。"
    if any(word in name for word in ("pair", "denied", "locked")):
        return "请解锁手机并确认信任此电脑，再重新连接。"
    if "module" in name or "import" in name:
        return "iOS 17 定位组件不完整，请重新安装软件。"
    return "iOS 17 USB 通道未完成，请检查连接、开发者模式和镜像准备所需的网络。"


async def serve(connection, stop, directory: Path):
    session = None
    owner = asyncio.current_task()

    async def watch_stop():
        while not stop.is_set():
            await asyncio.sleep(0.1)
        owner.cancel()

    watcher = asyncio.create_task(watch_stop())
    try:
        session = ModernSession(load_dependencies(directory))
        while not stop.is_set():
            if not connection.poll():
                await asyncio.sleep(0.05)
                continue
            request = connection.recv()
            try:
                operation = request.get("operation")
                if operation == "ping":
                    pass  # Dependency/spawn self-check; never contacts a device.
                elif operation == "prepare":
                    await session.prepare(request["serial"], request["version"])
                elif operation == "set":
                    await session.set(request["longitude"], request["latitude"])
                elif operation == "clear":
                    await session.clear()
                else:
                    raise DeviceError("无效的 iOS 定位操作。")
                connection.send({"ok": True})
            except Exception as exc:
                connection.send({"ok": False, "error": friendly_failure(exc)})
    except (EOFError, BrokenPipeError, OSError, asyncio.CancelledError):
        pass
    except Exception as exc:
        with suppress(OSError):
            connection.send({"ok": False, "error": friendly_failure(exc)})
    finally:
        watcher.cancel()
        if session is not None:
            with suppress(Exception):
                await asyncio.wait_for(session.close(), timeout=10)
        connection.close()


def modern_worker(connection, stop, directory):
    # Third-party diagnostics can include device identifiers. Do not relay them
    # to the desktop log or persist their stdout/stderr.
    import sys

    logging.disable(logging.CRITICAL)
    os.environ["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"
    os.environ["NO_PROXY"] = "*"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(key, None)
    with open(os.devnull, "w", encoding="utf-8") as sink:
        sys.stdout = sys.stderr = sink
        asyncio.run(serve(connection, stop, Path(directory)))


class ModernClient:
    def __init__(self, directory: Path):
        self.directory = directory / "modern"
        self.context = multiprocessing.get_context("spawn")
        self.process = self.connection = self.stop = None
        self.target = None

    def _start(self):
        if self.process is not None and self.process.is_alive():
            return
        self.close()
        self.directory.mkdir(parents=True, exist_ok=True)
        parent, child = self.context.Pipe()
        self.stop = self.context.Event()
        process = self.context.Process(
            target=modern_worker, args=(child, self.stop, str(self.directory)), daemon=True
        )
        try:
            process.start()
        except Exception:
            parent.close()
            child.close()
            raise
        child.close()
        self.process, self.connection = process, parent

    def _request(self, payload, timeout=20, cancel=None):
        if self.connection is None or self.process is None:
            raise DeviceError("iOS 17 通道已断开，请重新应用位置或恢复定位。")
        self.connection.send(payload)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                self.close()
                raise Cancelled()
            if self.connection.poll(0.1):
                response = self.connection.recv()
                if not response.get("ok"):
                    raise DeviceError(response.get("error", "iOS 定位操作未完成。"))
                return
            if not self.process.is_alive():
                break
        self.close()
        raise DeviceError("iOS 17 通道响应超时。请检查 USB 连接与镜像准备所需的网络。")

    def prepare(self, device, cancel, progress, timeout=180):
        self._start()
        progress("正在准备 iOS 17+ USB 隧道与开发者镜像，首次使用可能需要联网。")
        self._request(
            {
                "operation": "prepare",
                "serial": validate_serial(device.serial),
                "version": device.version,
            },
            timeout=timeout,
            cancel=cancel,
        )
        self.target = device

    def set_location(self, device, point, cancel):
        if self.target is None or self.target.serial != device.serial:
            raise DeviceError("iOS 17 通道与所选设备不匹配。")
        self._request(
            {"operation": "set", "latitude": point.latitude, "longitude": point.longitude},
            cancel=cancel,
        )

    def reset(self, device):
        try:
            if self.process is None or not self.process.is_alive():
                import threading

                self.prepare(device, threading.Event(), lambda _: None, timeout=40)
            if self.target is None or self.target.serial != device.serial:
                raise DeviceError("恢复目标与 iOS 17 通道不匹配。")
            self._request({"operation": "clear"}, timeout=12)
        finally:
            self.close()

    def close(self):
        if self.stop is not None:
            self.stop.set()
        if self.process is not None:
            self.process.join(timeout=12)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=2)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=2)
            self.process.close()
        if self.connection is not None:
            self.connection.close()
        self.process = self.connection = self.stop = self.target = None
