import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from zju_ac.ios.device import DeviceError
from zju_ac.ios.modern import ModernSession, friendly_failure

SERIAL = "00008110-0000000000000001"


@asynccontextmanager
async def context(value):
    yield value


def api_fixture():
    rsd = SimpleNamespace(udid=SERIAL, connect=AsyncMock(), close=AsyncMock())
    simulation = SimpleNamespace(set=AsyncMock(), clear=AsyncMock())
    mounter = SimpleNamespace(is_image_mounted=AsyncMock(return_value=True))
    tunnel_result = SimpleNamespace(
        address="fd00::1",
        port=1234,
        auxiliary_metadata={},
        client=SimpleNamespace(tun=SimpleNamespace(set_peer=Mock())),
    )
    provider = SimpleNamespace(
        rsd=rsd, close=AsyncMock(), start_quic_tunnel=Mock(return_value=context(tunnel_result))
    )
    api = {
        "tunnels": SimpleNamespace(
            USE_USERSPACE_TUNNEL=False,
            get_core_device_tunnel_services=AsyncMock(return_value=[provider]),
        ),
        "usb": Mock(return_value=context(rsd)),
        "rsd": Mock(return_value=rsd),
        "dial": Mock(return_value=context(SimpleNamespace(dial=Mock()))),
        "mounter": Mock(return_value=context(mounter)),
        "mount": AsyncMock(),
        "image_type": Mock(return_value="Personalized"),
        "dvt": Mock(return_value=context(object())),
        "location": Mock(return_value=context(simulation)),
    }
    return api, rsd, simulation, provider, mounter


@pytest.mark.parametrize("version", ["17.0", "17.3.1", "17.4", "18.5", "26.1"])
def test_both_modern_usb_routes_target_selected_device_and_restore(version):
    api, _, simulation, provider, _ = api_fixture()

    async def run():
        session = ModernSession(api)
        await session.prepare(SERIAL, version)
        await session.set(120.1, 30.2)
        await session.close()

    asyncio.run(run())
    simulation.set.assert_awaited_once_with(30.2, 120.1)
    simulation.clear.assert_awaited_once()
    if tuple(map(int, version.split(".")[:2])) >= (17, 4):
        api["usb"].assert_called_once_with(
            serial=SERIAL, autopair=True, remotepairing_fallback=False
        )
        provider.start_quic_tunnel.assert_not_called()
    else:
        api["tunnels"].get_core_device_tunnel_services.assert_awaited_once_with(udid=SERIAL)
        provider.start_quic_tunnel.assert_called_once()
        api["usb"].assert_not_called()


def test_wrong_rsd_device_is_rejected_before_mount_or_location_write():
    api, rsd, simulation, _, _ = api_fixture()
    rsd.udid = "00008110-0000000000000002"
    with pytest.raises(DeviceError, match="标识"):
        asyncio.run(ModernSession(api).prepare(SERIAL, "18.0"))
    api["mount"].assert_not_awaited()
    simulation.set.assert_not_awaited()


def test_personalized_image_is_only_mounted_when_missing():
    api, _, simulation, _, mounter = api_fixture()
    mounter.is_image_mounted.return_value = False

    async def run():
        session = ModernSession(api)
        await session.prepare(SERIAL, "17.4")
        await session.close()

    asyncio.run(run())
    api["mount"].assert_awaited_once()
    simulation.set.assert_not_awaited()


def test_uncertain_modern_write_is_restored_when_helper_closes():
    api, _, simulation, _, _ = api_fixture()
    simulation.set.side_effect = RuntimeError("private-device-coordinate")

    async def run():
        session = ModernSession(api)
        await session.prepare(SERIAL, "17.1")
        with pytest.raises(RuntimeError):
            await session.set(120, 30)
        await session.close()

    asyncio.run(run())
    simulation.clear.assert_awaited_once()
    assert "private" not in friendly_failure(RuntimeError("private-device-coordinate"))


def test_missing_old_usb_interface_does_not_fall_back_to_wifi_or_write():
    api, _, simulation, _, _ = api_fixture()
    api["tunnels"].get_core_device_tunnel_services.return_value = []
    with pytest.raises(DeviceError, match="USB"):
        asyncio.run(ModernSession(api).prepare(SERIAL, "17.2"))
    api["usb"].assert_not_called()
    simulation.set.assert_not_awaited()
