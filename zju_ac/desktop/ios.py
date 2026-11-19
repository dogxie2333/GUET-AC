"""Nonblocking USB-location extension, independent of account monitoring."""

import shutil
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Property, QObject, QThread, QTimer, Signal, Slot

from ..diagnostics import record
from ..ios import drivers
from ..ios.device import Cancelled, DeviceBackend, DeviceError, resource_directory
from ..ios.locations import coordinates, load_locations, locations_file


class DeviceJob(QThread):
    progress = Signal(str)

    def __init__(self, backend, operation, target=None, point=None, parent=None, automatic=False):
        super().__init__(parent)
        self.backend, self.operation, self.target, self.point = backend, operation, target, point
        self.cancel = threading.Event()
        self.automatic = automatic
        self.modified = None
        self.value, self.error, self.cancelled = None, "", False
        self.exception = None

    def run(self):
        if not self.automatic:
            record("ios_started", operation=self.operation)
        try:
            if self.operation == "scan":
                self.value = self.backend.discover(self.cancel, refresh=not self.automatic)
            elif self.operation == "apply":
                target = self.backend.prepare(self.target, self.cancel, self.progress.emit)
                if self.cancel.is_set():
                    raise Cancelled()
                # A failed/timeout write can still have affected the phone.
                self.modified = target
                self.backend.set_location(target, self.point, self.cancel)
            elif self.operation == "reset":
                self.backend.reset(self.target)
            if not self.automatic:
                record("ios_finished", operation=self.operation, outcome="success")
        except Cancelled:
            self.cancelled = True
            record("ios_finished", operation=self.operation, action="cancel")
        except Exception as exc:
            self.exception = exc
            self.error = (
                str(exc) if isinstance(exc, DeviceError) else "USB 定位操作异常，请导出诊断包。"
            )
            if not self.automatic:
                record("ios_error", operation=self.operation, error=exc)


class IosController(QObject):
    changed = Signal()
    devicesChanged = Signal()
    locationsChanged = Signal()
    shutdownFinished = Signal()
    restoreFailed = Signal()

    def __init__(self, directory: Path, parent=None, backend=None, template=None):
        super().__init__(parent)
        self.directory = directory / "ios"
        self.backend = backend or DeviceBackend(self.directory)
        self._job = None
        self._devices, self._locations = [], []
        self._selected_index = -1
        self._detecting = False
        self._last_scan_error = ""
        self.scan_timer = QTimer(self)
        self.scan_timer.setInterval(5000)
        self.scan_timer.timeout.connect(self._auto_scan)
        self._restore_device = None
        self._status, self._message = "等待连接", "自动检测 USB 设备，请连接并解锁 iPhone。"
        self._closing = self._exit_reset_attempted = self._applied = False
        self._driver_handle = None
        self.driver_timer = QTimer(self)
        self.driver_timer.setInterval(1000)
        self.driver_timer.timeout.connect(self._poll_driver)
        self.locations_path = (
            locations_file(directory) if template is None else self.directory / "location.md"
        )
        source_location = Path(__file__).resolve().parents[2] / "location.md"
        if template is None and not getattr(sys, "frozen", False):
            self.locations_path = source_location
        seed = template or (
            resource_directory() / "location.md"
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[2] / "location.md"
        )
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if not self.locations_path.exists() and seed.is_file():
                shutil.copyfile(seed, self.locations_path)
        except OSError:
            self._message = "USB 定位数据目录无法写入，请检查 data/ios 的权限。"
        self.reloadLocations()

    @Property("QVariantMap", notify=changed)
    def state(self):
        return {
            "busy": self._job is not None,
            "active": self._restore_device is not None,
            "applied": self._applied,
            "status": self._status,
            "message": self._message,
            "driverInstalling": self._driver_handle is not None,
            "closing": self._closing,
            "selectedIndex": self._selected_index,
        }

    @Property("QVariantList", notify=devicesChanged)
    def devices(self):
        return [{"name": f"{d.name} · iOS {d.version} · …{d.serial[-6:]}"} for d in self._devices]

    @Property("QVariantList", notify=locationsChanged)
    def locations(self):
        return self._locations

    @Slot()
    def reloadLocations(self):
        try:
            self._locations = (
                load_locations(self.locations_path) if self.locations_path.exists() else []
            )
        except Exception:
            self._message = "location.md 无法读取，请检查 地点：经度,纬度 格式。"
        self.locationsChanged.emit()
        self.changed.emit()

    def _start(self, operation, target=None, point=None, automatic=False):
        if self._job is not None:
            return
        self._job = DeviceJob(self.backend, operation, target, point, self, automatic=automatic)
        self._job.progress.connect(self._progress)
        self._job.finished.connect(self._finished)
        if not automatic:
            self._status = {"scan": "正在检测", "apply": "正在应用", "reset": "正在恢复"}[operation]
            self._message = "请保持手机解锁和 USB 连接。"
        self._job.start()
        self.changed.emit()

    @Slot(str)
    def _progress(self, message):
        self._message = message
        self.changed.emit()

    @Slot()
    def scan(self):
        if not self._closing:
            self._start("scan")

    def startDetection(self):
        self._detecting = True
        self.scan_timer.start()
        QTimer.singleShot(0, self._auto_scan)

    @Slot()
    def _auto_scan(self):
        if (
            self._detecting
            and not self._closing
            and self._job is None
            and self._driver_handle is None
        ):
            self._start("scan", automatic=True)

    @Slot(int)
    def selectDevice(self, index):
        if self._job is None:
            self._selected_index = index if 0 <= index < len(self._devices) else -1
            self.changed.emit()

    @Slot(int, str, str, str)
    def applyLocation(self, index, longitude, latitude, system):
        if self._job is not None or self._closing:
            return
        try:
            if index < 0 or index >= len(self._devices):
                raise DeviceError("请先检测并选择 USB 设备。")
            point = coordinates(longitude, latitude, system)
            target = self._devices[index]
            if self._restore_device and self._restore_device.serial != target.serial:
                raise DeviceError("请先恢复当前设备的真实定位，再切换到另一台设备。")
        except Exception as exc:
            self._message = (
                str(exc)
                if isinstance(exc, (ValueError, DeviceError))
                else "经纬度无效，请检查输入。"
            )
            self.changed.emit()
            return
        self._start("apply", target, point)

    @Slot()
    def resetLocation(self):
        if self._restore_device is not None and self._job is None:
            self._start("reset", self._restore_device)

    @Slot()
    def _finished(self):
        job, self._job = self._job, None
        if job.automatic and job.error != self._last_scan_error:
            if job.error:
                record("ios_error", operation="scan", error=job.exception)
            self._last_scan_error = job.error
        if job.modified is not None:
            self._restore_device = job.modified
            self._applied = not job.error and not job.cancelled
        if job.operation == "scan" and not job.error and not job.cancelled:
            selected = (
                self._devices[self._selected_index].serial
                if 0 <= self._selected_index < len(self._devices)
                else None
            )
            changed = self._devices != job.value
            first_connection = not self._devices
            self._devices = job.value
            self._selected_index = next(
                (i for i, d in enumerate(self._devices) if d.serial == selected),
                0 if first_connection and selected is None and len(self._devices) == 1 else -1,
            )
            if changed:
                self.devicesChanged.emit()
                record("ios_detected", count=len(self._devices))
            if job.automatic and not changed:
                job.deleteLater()
                self.changed.emit()
                if self._closing:
                    self.requestShutdown()
                return
        if job.operation == "reset" and not job.error:
            self._restore_device = None
            self._applied = False
        self._status = "待处理" if job.error else "已就绪"
        self._message = (
            job.error
            or {
                "scan": "检测完成，请选择设备。"
                if self._devices
                else "未检测到 USB 设备，请检查连接并解锁手机。",
                "apply": "模拟位置已发送到所选设备。关闭窗口后仍会保持；点击恢复可还原真实定位。",
                "reset": "已发送恢复真实定位指令。",
            }[job.operation]
        )
        if job.cancelled:
            self._message = "正在停止 USB 定位任务。"
        job.deleteLater()
        self.changed.emit()
        if self._closing:
            if job.operation == "reset" and job.error:
                self.restoreFailed.emit()
            self.requestShutdown()

    @Slot()
    def installDrivers(self):
        if self._driver_handle is not None or self._closing or self._job is not None:
            return
        try:
            self._driver_handle = drivers.launch_installer()
            self._message = "请在 Windows 授权窗口中确认。驱动安装完成后，重新检测设备。"
            self.driver_timer.start()
        except Exception:
            self._message = "未启动驱动安装，请确认管理员授权后重试。"
        self.changed.emit()

    @Slot()
    def _poll_driver(self):
        try:
            code = drivers.installation_result(self._driver_handle)
        except Exception:
            code = 1
        if code is None:
            return
        drivers.close_handle(self._driver_handle)
        self._driver_handle = None
        self.driver_timer.stop()
        self._message = {
            0: "Apple USB 组件已准备完成，请检测设备。",
            3010: "Apple USB 组件已安装，请重启 Windows。",
        }.get(code, "驱动安装未完成，可重新尝试安装。")
        self.changed.emit()

    @Slot()
    def requestShutdown(self):
        self._closing = True
        self._detecting = False
        self.scan_timer.stop()
        if self._job is not None:
            self._job.cancel.set()
            return
        if self._restore_device is not None and not self._exit_reset_attempted:
            self._exit_reset_attempted = True
            self._start("reset", self._restore_device)
        else:
            self.shutdownFinished.emit()

    def finalize(self):
        self._detecting = False
        self.scan_timer.stop()
        self.driver_timer.stop()
        if self._driver_handle is not None:
            drivers.close_handle(self._driver_handle)
            self._driver_handle = None
        if self._job is not None:
            job = self._job
            job.cancel.set()
            job.wait(20000)
            if job.modified is not None:
                self._restore_device = job.modified
        if self._restore_device is not None and not self._exit_reset_attempted:
            try:
                self.backend.reset(self._restore_device)
            except Exception as exc:
                record("ios_error", operation="reset", error=exc)
        self.backend.close()
