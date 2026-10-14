"""Tests for receive timestamps, the restored marker and BoilerSnapshot.readings."""
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import callback

from custom_components.sat.coordinator import Reading
from custom_components.sat.fake import SatFakeCoordinator
from tests.conftest import advance
from tests.test_coordinator import STORE_KEY

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen(freezer):
    freezer.move_to(T0)


def restore(hass_storage, **data) -> None:
    hass_storage[STORE_KEY] = {"version": 1, "key": STORE_KEY, "data": data}


# Live values

async def test_live_values_are_stamped_with_the_receive_time(ems, boiler_data):
    await boiler_data(selflowtemp=55, heatingactive="on")

    assert dict(ems.data.readings()) == {
        "selflowtemp": Reading(value=55, received_at=T0),
        "heatingactive": Reading(value="on", received_at=T0),
    }
    assert not ems.data.readings()["selflowtemp"].restored


async def test_unchanged_value_moves_the_time_forward_without_notifying(hass, ems, boiler_data, freezer):
    notified = []
    ems.async_add_listener(callback(lambda: notified.append(1)))
    await boiler_data(selflowtemp=55)
    await advance(hass, freezer, 5)
    assert len(notified) == 1

    await boiler_data(selflowtemp=55)
    await advance(hass, freezer, 60)

    assert ems.data.readings()["selflowtemp"] == Reading(value=55, received_at=T0 + timedelta(seconds=5))
    assert len(notified) == 1


async def test_only_received_keys_get_a_new_time(hass, ems, boiler_data, freezer):
    await boiler_data(selflowtemp=55, rettemp=40)
    await advance(hass, freezer, 10)
    await boiler_data(selflowtemp=56)

    readings = ems.data.readings()
    assert readings["selflowtemp"].received_at == T0 + timedelta(seconds=10)
    assert readings["rettemp"].received_at == T0


# Restored values

async def test_restored_values_have_no_time_and_are_marked_restored(hass_storage, ems):
    restore(hass_storage, selflowtemp=50, rettemp=40)

    await ems.async_setup()

    readings = ems.data.readings()
    assert readings["selflowtemp"] == Reading(value=50, received_at=None)
    assert readings["selflowtemp"].restored and readings["rettemp"].restored


@pytest.mark.parametrize("live_value", [56, 50])
async def test_live_value_clears_only_its_own_restored_marker(hass_storage, ems, boiler_data, live_value):
    restore(hass_storage, selflowtemp=50, rettemp=40)
    await ems.async_setup()

    await boiler_data(selflowtemp=live_value)

    readings = ems.data.readings()
    assert readings["selflowtemp"] == Reading(value=live_value, received_at=T0)
    assert not readings["selflowtemp"].restored
    assert readings["rettemp"].restored


async def test_saved_storage_contains_plain_values_only(hass_storage, ems, boiler_data):
    restore(hass_storage, rettemp=40)
    await ems.async_setup()
    await boiler_data(selflowtemp=55)

    await ems.async_will_remove_from_hass()

    assert hass_storage[STORE_KEY]["data"] == {"rettemp": 40, "selflowtemp": 55}


# Snapshot

async def test_snapshot_readings_match_data_and_ignore_later_messages(ems, boiler_data, freezer):
    await boiler_data(selflowtemp=55)
    snapshot = ems.snapshot()

    freezer.tick(10)
    await boiler_data(selflowtemp=60, rettemp=40)

    assert dict(snapshot.readings) == {"selflowtemp": Reading(value=55, received_at=T0)}
    assert ems.snapshot().readings["selflowtemp"] == Reading(value=60, received_at=T0 + timedelta(seconds=10))


async def test_snapshot_readings_are_read_only(ems, boiler_data):
    await boiler_data(selflowtemp=55)
    readings = ems.snapshot().readings

    with pytest.raises(TypeError):
        readings["selflowtemp"] = Reading(value=1, received_at=None)  # type: ignore[index]
    with pytest.raises(TypeError):
        del readings["selflowtemp"]  # type: ignore[attr-defined]


async def test_snapshot_readings_empty_without_device_data(hass):
    assert not SatFakeCoordinator(hass, {}).snapshot().readings
