import json
import plistlib
import threading
from unittest.mock import Mock

import pytest

from zju_ac.ios import drivers
from zju_ac.ios.device import Cancelled, Device, DeviceBackend, DeviceError, parse_devices
from zju_ac.ios.locations import coordinates, load_locations

SERIAL = "00008110-0000000000000001"
PHONE = Device(SERIAL, "Test phone", "16.7.12")


@pytest.mark.parametrize(
    "lon,lat", [("nan", "30"), ("120", "inf"), ("181", "30"), ("120", "91"), ("bad", "30")]
)
def test_invalid_coordinates_do_not_become_native_arguments(lon, lat):
    with pytest.raises(ValueError):
        coordinates(lon, lat)


def test_gcj_conversion_and_explicit_wgs84():
    point = coordinates("116.404", "39.915", "gcj02")
    assert point.longitude == pytest.approx(116.3977555, abs=1e-5)
    assert point.latitude == pytest.approx(39.9135957, abs=1e-5)
    assert coordinates("120", "30", "wgs84").longitude == 120
    assert coordinates("-74", "40", "gcj02").longitude == -74


def test_location_file_preserves_longitude_latitude_order_and_group(tmp_path):
    path = tmp_path / "location.md"
    path.write_text("\ufeff紫金港：\n北1：120.1,30.2\n---\n玉泉：\n教1：120.2,30.3", "utf-8")
    locations = load_locations(path)
    assert locations[0] == {"name": "紫金港 · 北1", "longitude": "120.1", "latitude": "30.2"}
    assert locations[1]["name"] == "玉泉 · 教1"


def test_discovery_uses_usb_only_and_rejects_invalid_identifiers():
    assert parse_devices(
        {
            "DeviceList": [
                {"Properties": {"ConnectionType": "Network", "SerialNumber": SERIAL}},
                {"Properties": {"ConnectionType": "USB", "SerialNumber": SERIAL}},
            ]
        }
    ) == [SERIAL]
    with pytest.raises(DeviceError):
        parse_devices(
            {
                "DeviceList": [
                    {"Properties": {"ConnectionType": "USB", "SerialNumber": "../../other"}}
                ]
            }
        )


def test_prepare_pins_pair_mount_and_location_to_selected_device(tmp_path):
    backend = DeviceBackend(tmp_path)
    backend.probe = Mock(return_value=PHONE)
    backend.mounted = Mock(return_value=True)
    backend.command = Mock(return_value="Developer mode is enabled")
    backend.prepare(PHONE, threading.Event(), Mock())
    backend.set_location(PHONE, coordinates("120", "30", "wgs84"), threading.Event())
    backend.reset(PHONE)
    for call in backend.command.call_args_list:
        assert call.args[1:3] == ("-u", SERIAL)
    assert backend.command.call_args_list[-2].args[-3:] == ("--", "30.00000000", "120.00000000")
    assert backend.command.call_args.args[-1] == "reset"


def test_ios17_delegates_to_separate_session_without_legacy_tools(tmp_path):
    backend = DeviceBackend(tmp_path)
    backend.probe = Mock(return_value=Device(SERIAL, "Test phone", "17.4"))
    backend.command = Mock()
    backend._modern = Mock()
    device = backend.prepare(PHONE, threading.Event(), Mock())
    backend.set_location(device, coordinates("120", "30", "wgs84"), threading.Event())
    backend.reset(device)
    backend._modern.prepare.assert_called_once()
    backend._modern.set_location.assert_called_once()
    backend._modern.reset.assert_called_once_with(device)
    backend.command.assert_not_called()


def test_automatic_discovery_caches_unchanged_devices_and_refreshes_after_reconnect(
    tmp_path, monkeypatch
):
    backend = DeviceBackend(tmp_path)
    monkeypatch.setattr(
        "zju_ac.ios.device.usb_serials", Mock(side_effect=[[SERIAL], [SERIAL], [], [SERIAL]])
    )
    backend.probe = Mock(return_value=PHONE)
    for _ in range(4):
        backend.discover(threading.Event())
    assert backend.probe.call_count == 2


def test_device_switch_cannot_be_silently_accepted(tmp_path):
    backend = DeviceBackend(tmp_path)
    backend.command = Mock(
        return_value=plistlib.dumps(
            {
                "UniqueDeviceID": "00008110-0000000000000002",
                "ProductVersion": "16.7",
            }
        ).decode()
    )
    with pytest.raises(DeviceError, match="标识"):
        backend.probe(SERIAL, threading.Event())


def test_prepare_uses_user_image_minor_version_then_verifies_mount(tmp_path):
    folder = tmp_path / "images/16.7"
    folder.mkdir(parents=True)
    for name in ("DeveloperDiskImage.dmg", "DeveloperDiskImage.dmg.signature"):
        (folder / name).write_bytes(b"test")
    backend = DeviceBackend(tmp_path)
    backend.probe = Mock(return_value=PHONE)
    backend.mounted = Mock(side_effect=[False, True])
    backend.command = Mock(return_value="enabled")
    backend.prepare(PHONE, threading.Event(), Mock())
    mounted = backend.command.call_args
    assert mounted.args[:3] == ("ideviceimagemounter", "-u", SERIAL)
    assert str(folder / "DeveloperDiskImage.dmg") in mounted.args


def test_tampered_tools_are_never_started(tmp_path, monkeypatch):
    (tmp_path / "win").mkdir()
    (tmp_path / "win/ideviceinfo.exe").write_bytes(b"bad")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "files": [
                    {"name": "ideviceinfo.exe", "sha256": "0" * 64},
                ]
            }
        ),
        "utf-8",
    )
    process = Mock()
    monkeypatch.setattr("zju_ac.ios.device.subprocess.Popen", process)
    with pytest.raises(DeviceError, match="校验"):
        DeviceBackend(tmp_path, tmp_path).command("ideviceinfo")
    process.assert_not_called()


def test_cancel_kills_only_owned_native_process(tmp_path, monkeypatch):
    backend = DeviceBackend(tmp_path)
    backend.verify_tools = Mock()
    cancel = threading.Event()
    process = Mock()
    process.poll.return_value = None

    def communicate(**kwargs):
        if not cancel.is_set():
            cancel.set()
            import subprocess

            raise subprocess.TimeoutExpired("test", 0.15)
        return b"", b""

    process.communicate.side_effect = communicate
    monkeypatch.setattr("zju_ac.ios.device.subprocess.Popen", Mock(return_value=process))
    with pytest.raises(Cancelled):
        backend.command("ideviceinfo", cancel=cancel)
    process.kill.assert_called_once()


def test_driver_signature_failure_prevents_extraction_or_install(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDIR", "C:/Windows")
    monkeypatch.setattr(drivers, "components_present", lambda: False)
    monkeypatch.setattr(drivers, "download", Mock())
    monkeypatch.setattr(drivers, "verify_signature", Mock(side_effect=RuntimeError("signature")))
    run = Mock()
    monkeypatch.setattr(drivers, "run", run)
    with pytest.raises(RuntimeError):
        drivers.install(tmp_path)
    run.assert_not_called()


def test_existing_apple_components_are_not_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDIR", "C:/Windows")
    monkeypatch.setattr(drivers, "components_present", lambda: True)
    fetch, run = Mock(), Mock(return_value=0)
    monkeypatch.setattr(drivers, "download", fetch)
    monkeypatch.setattr(drivers, "run", run)
    assert drivers.install(tmp_path) == 0
    fetch.assert_not_called()
    assert run.call_args.args[0][-2:] == ["start", "Apple Mobile Device Service"]
