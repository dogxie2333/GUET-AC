import os
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtWidgets import QApplication

from zju_ac.config import CONFIG_PATH
from zju_ac.desktop.bridge import DesktopBridge
from zju_ac.desktop.ios import IosController
from zju_ac.desktop.storage import ConfigStore
from zju_ac.ios.device import Device

PHONE = Device("00008110-0000000000000001", "Test phone", "16.7")


def wait_idle(app, controller):
    deadline = time.monotonic() + 3
    while controller.state["busy"] and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert not controller.state["busy"]


@pytest.fixture
def ios(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("zju_ac.desktop.ios.record", lambda *a, **kw: None)
    backend = Mock()
    backend.discover.return_value = [PHONE]
    backend.prepare.return_value = PHONE
    template = tmp_path / "locations.md"
    template.write_text("测试：120.1,30.2", "utf-8")
    controller = IosController(tmp_path, backend=backend, template=template)
    yield app, controller, backend
    controller.finalize()
    controller.deleteLater()
    app.processEvents()


def test_startup_never_contacts_phone_and_apply_requires_explicit_action(ios):
    app, controller, backend = ios
    backend.discover.assert_not_called()
    backend.set_location.assert_not_called()
    controller.applyLocation(0, "120", "30", "wgs84")
    backend.prepare.assert_not_called()
    controller.scan()
    wait_idle(app, controller)
    assert len(controller.devices) == 1
    backend.set_location.assert_not_called()
    controller.applyLocation(0, "120", "30", "wgs84")
    wait_idle(app, controller)
    point = backend.set_location.call_args.args[1]
    assert point.longitude == 120 and point.latitude == 30
    assert controller.state["active"]
    ready = Mock()
    controller.shutdownFinished.connect(ready)
    controller.requestShutdown()
    wait_idle(app, controller)
    backend.reset.assert_called_once_with(PHONE)
    ready.assert_called_once()


def test_automatic_detection_starts_without_applying_and_stops_on_shutdown(ios):
    app, controller, backend = ios
    controller.startDetection()
    app.processEvents()
    wait_idle(app, controller)
    assert controller.scan_timer.isActive()
    backend.discover.assert_called_once()
    assert controller.state["selectedIndex"] == 0
    backend.prepare.assert_not_called()
    backend.set_location.assert_not_called()
    controller.requestShutdown()
    assert not controller.scan_timer.isActive()
    controller._auto_scan()
    assert backend.discover.call_count == 1


def test_selection_survives_reorder_and_never_silently_switches_on_disconnect(ios):
    app, controller, backend = ios
    other = Device("00008110-0000000000000002", "Other phone", "17.2")
    backend.discover.return_value = [PHONE, other]
    controller.scan()
    wait_idle(app, controller)
    assert controller.state["selectedIndex"] == -1
    controller.selectDevice(1)
    backend.discover.return_value = [other, PHONE]
    controller.scan()
    wait_idle(app, controller)
    assert controller.state["selectedIndex"] == 0
    backend.discover.return_value = [PHONE]
    controller.scan()
    wait_idle(app, controller)
    assert controller.state["selectedIndex"] == -1
    # Repeated automatic polls must not select the remaining phone either.
    controller._start("scan", automatic=True)
    wait_idle(app, controller)
    assert controller.state["selectedIndex"] == -1


def test_uncertain_location_write_still_offers_restore(ios):
    app, controller, backend = ios
    controller._devices = [PHONE]
    backend.set_location.side_effect = RuntimeError("private-coordinate-device-id")
    controller.applyLocation(0, "120", "30", "gcj02")
    wait_idle(app, controller)
    assert controller.state["active"]
    assert "private-coordinate" not in controller.state["message"]
    controller.resetLocation()
    wait_idle(app, controller)
    assert not controller.state["active"]


def test_cannot_switch_devices_until_previous_location_is_restored(ios):
    _, controller, backend = ios
    other = Device("00008110-0000000000000002", "Other phone", "16.6")
    controller._devices = [other]
    controller._restore_device = PHONE
    controller.applyLocation(0, "120", "30", "wgs84")
    backend.prepare.assert_not_called()
    assert "先恢复" in controller.state["message"]


def test_shutdown_after_uncertain_write_restores_same_device_once(ios):
    app, controller, backend = ios
    controller._devices = [PHONE]
    backend.set_location.side_effect = RuntimeError("unknown")
    controller.applyLocation(0, "120", "30", "wgs84")
    wait_idle(app, controller)
    controller.requestShutdown()
    wait_idle(app, controller)
    backend.reset.assert_called_once_with(PHONE)


def test_shutdown_during_prepare_cancels_before_location_write(ios):
    app, controller, backend = ios
    controller._devices = [PHONE]

    def prepare(target, cancel, progress):
        cancel.wait(timeout=1)
        return target

    backend.prepare.side_effect = prepare
    controller.applyLocation(0, "120", "30", "wgs84")
    controller.requestShutdown()
    wait_idle(app, controller)
    backend.set_location.assert_not_called()
    backend.reset.assert_not_called()


def test_qml_extension_resizes_without_automatically_touching_device(ios, tmp_path, monkeypatch):
    app, controller, backend = ios
    monkeypatch.setattr("zju_ac.desktop.bridge.setup", lambda *a: None)
    bridge = DesktopBridge(ConfigStore(tmp_path / "main", CONFIG_PATH))
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", bridge)
    engine.rootContext().setContextProperty("iosBackend", controller)
    warnings = []
    engine.warnings.connect(lambda items: warnings.extend(str(x) for x in items))
    engine.load(QUrl.fromLocalFile(str(Path(__file__).parents[1] / "zju_ac/desktop/qml/Main.qml")))
    try:
        assert engine.rootObjects()
        window = engine.rootObjects()[0]
        window.setProperty("page", 3)
        window.setWidth(620)
        window.setHeight(480)
        window.show()
        app.processEvents()
        assert window.findChild(QObject, "iosLongitude") is not None
        assert window.findChild(QObject, "iosLatitude") is not None
        assert not window.findChild(QObject, "iosApply").property("enabled")
        scroll = window.findChild(QQuickItem, "mainScroll").property("contentItem")
        scroll.setProperty("contentY", 100)
        window.setProperty("page", 0)
        app.processEvents()
        window.setProperty("page", 3)
        app.processEvents()
        app.processEvents()
        assert scroll.property("contentY") == 0
        backend.discover.assert_not_called()
        backend.set_location.assert_not_called()
        assert not warnings
        window.hide()
    finally:
        bridge.shutdown()
        engine.deleteLater()
        app.processEvents()
