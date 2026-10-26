"""Channel synchro sensors, checked through `hass.states` after device messages and control cycles."""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from custom_components.sat.channels import ChannelStatus, DesiredState, Quantity
from custom_components.sat.const import CLIMATE, DIAGNOSTIC_WINDOW, DOMAIN
from custom_components.sat.coordinator import DeviceState
from custom_components.sat.sensor import SatChannelSynchroSensor, async_setup_entry
from tests.conftest import advance
from tests.const import EMS_BOILER_DATA, IDLE, ems_entry, otgw_entry
from tests.test_mqtt_ems import cycle

pytestmark = pytest.mark.usefixtures("loop_entry")

CHANNELS = ["control_setpoint", "maximum_relative_modulation", "heater_state", "maximum_setpoint"]
HEALTH = {"test-flame-health", "test-boiler-health"}
LEGACY_SUFFIXES = ("-control-setpoint-synchro", "-relative-modulation-synchro", "-central-heating-synchro")
OPTIONS = ["idle", "waiting", "in_sync", "out_of_sync", "drifted", "write_only", "read_only"]
SETPOINT = Quantity.CONTROL_SETPOINT


def unique_ids(hass: HomeAssistant, entry, domain: str) -> set[str]:
    registry = er.async_get(hass)

    return {e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id) if e.domain == domain}


def shown(hass: HomeAssistant, name: str = "control_setpoint"):
    return hass.states.get(f"sensor.{name}_synchro")


async def step_desired(coordinator, boiler_data, freezer, desired: DesiredState, **data) -> None:
    """Receive device data a second after the previous step, then run one channel cycle."""
    freezer.tick(1)
    await boiler_data(**data)
    await cycle(coordinator, desired)


async def started(coordinator, climate, boiler_data) -> float:
    """Run the first control loop in HEAT mode and return the control setpoint it wrote."""
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()

    return coordinator.channel_states[SETPOINT].sent


async def step(climate, boiler_data, freezer, **data) -> None:
    """Receive device data a second after the previous step, then run one control loop."""
    freezer.tick(1)
    await boiler_data(**data)
    await climate.async_control_heating_loop()


# Which entities exist

@pytest.mark.parametrize(*ems_entry())
async def test_ems_has_channel_sensors(hass, entry):
    assert unique_ids(hass, entry, "sensor") >= {f"test-channel-{name}" for name in CHANNELS}
    assert unique_ids(hass, entry, "binary_sensor") >= HEALTH

    for name in CHANNELS:
        state = shown(hass, name)
        assert state is not None
        assert state.attributes["options"] == OPTIONS
        assert state.attributes["device_class"] == "enum"
        assert state.state in OPTIONS
        # The channel details are not state attributes
        assert set(state.attributes) == {"options", "device_class", "friendly_name"}

    registry = er.async_get(hass)
    for name in CHANNELS:
        registered = registry.async_get(f"sensor.{name}_synchro")
        assert registered.unique_id == f"test-channel-{name}"
        assert registered.entity_category == er.EntityCategory.DIAGNOSTIC
        assert registered.disabled_by is None
    assert shown(hass, "heater_state").attributes["friendly_name"].endswith("Heater State Synchro")
    assert shown(hass, "maximum_relative_modulation").attributes["friendly_name"].endswith("Maximum Relative Modulation Synchro")


@pytest.mark.parametrize(*otgw_entry())
async def test_backend_without_channels_has_no_channel_sensors(hass, entry):
    assert not {uid for uid in unique_ids(hass, entry, "sensor") if "-channel-" in uid}
    assert unique_ids(hass, entry, "binary_sensor") >= HEALTH
    assert shown(hass) is None
    assert not {uid for uid in unique_ids(hass, entry, "binary_sensor") if uid.endswith(LEGACY_SUFFIXES)}


@pytest.mark.parametrize(*ems_entry())
async def test_channel_sensors_exist_when_the_climate_is_not_registered(hass, entry):
    added = []
    del hass.data[DOMAIN][entry.entry_id][CLIMATE]

    await async_setup_entry(hass, entry, added.extend)

    assert sorted(e.unique_id for e in added if isinstance(e, SatChannelSynchroSensor)) == sorted(f"test-channel-{n}" for n in CHANNELS)


# State and the signal

@pytest.mark.parametrize(*ems_entry())
async def test_cycle_updates_the_states_through_the_signal_before_any_debounce(hass, entry, coordinator, boiler_data, freezer):
    desired = DesiredState(control_setpoint=40, heater_state=DeviceState.OFF)
    await boiler_data(**EMS_BOILER_DATA)
    assert shown(hass).state != "in_sync"
    assert shown(hass, "heater_state").state != "in_sync"

    # No time passes in this test, so the debounced notification cannot have fired
    await cycle(coordinator, desired)
    assert shown(hass).state == "in_sync"
    assert shown(hass, "heater_state").state == "in_sync"
    assert coordinator.channel_states[SETPOINT].sent == 40

    await step_desired(coordinator, boiler_data, freezer, desired, selflowtemp=40, heatingoff="on")
    assert (shown(hass).state, shown(hass, "heater_state").state) == ("in_sync", "in_sync")


@pytest.mark.parametrize(*ems_entry())
async def test_coordinator_verify_alone_signals_the_synchro_entities(hass, entry, coordinator, climate, boiler_data, freezer):
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await boiler_data(**EMS_BOILER_DATA)
    await cycle(coordinator, DesiredState(control_setpoint=40))
    assert shown(hass).state == "in_sync"

    # No time passes, so only the signal can have updated the state
    freezer.tick(1)
    await boiler_data(selflowtemp=50)
    assert shown(hass).state == "in_sync"
    # A verify without a submit
    coordinator.verify(coordinator.snapshot())
    await hass.async_block_till_done()

    assert coordinator.channel_states[SETPOINT].sent == 40
    assert shown(hass).state == "out_of_sync"


# Held problems. The climate's own loop keeps running while time advances, so these use it instead of direct channel cycles.

@pytest.mark.parametrize(*ems_entry())
async def test_drift_shows_and_drops_back_to_in_sync_through_the_timer(hass, entry, coordinator, climate, boiler_data, freezer):
    value = await started(coordinator, climate, boiler_data)
    await step(climate, boiler_data, freezer, selflowtemp=value)
    assert shown(hass).state == "in_sync"

    await step(climate, boiler_data, freezer, selflowtemp=value + 10)
    assert (shown(hass).state, coordinator.channel_states[SETPOINT].drift_count) == ("drifted", 1)

    # The rewrite is read back, but the drift keeps showing
    await step(climate, boiler_data, freezer, selflowtemp=value)
    assert shown(hass).state == "drifted"
    assert coordinator.channel_states[SETPOINT].status == ChannelStatus.IN_SYNC
    expires_at = coordinator.channel_states[SETPOINT].last_drift_at + DIAGNOSTIC_WINDOW

    # Flush the debounced notification; from here no cycle changes the channel, only the timer changes the state
    await advance(hass, freezer, 10)
    await advance(hass, freezer, (expires_at - dt_util.utcnow()).total_seconds() - 1)
    assert shown(hass).state == "drifted"

    await advance(hass, freezer, 1)
    assert shown(hass).state == "in_sync"


@pytest.mark.parametrize(*ems_entry())
async def test_held_out_of_sync_expires_into_a_still_held_drift(hass, entry, coordinator, climate, boiler_data, freezer):
    value = await started(coordinator, climate, boiler_data)

    # A first read-back that differs is out of sync, then it confirms, then the boiler reverts: a drift
    await step(climate, boiler_data, freezer, selflowtemp=value + 10)
    assert shown(hass).state == "out_of_sync"
    await step(climate, boiler_data, freezer, selflowtemp=value)
    await step(climate, boiler_data, freezer, selflowtemp=value + 10)
    await step(climate, boiler_data, freezer, selflowtemp=value)
    channel = coordinator.channel_states[SETPOINT]
    assert channel.status == ChannelStatus.IN_SYNC
    assert shown(hass).state == "out_of_sync"
    out_of_sync_ends = channel.last_out_of_sync_at + DIAGNOSTIC_WINDOW
    drift_ends = channel.last_drift_at + DIAGNOSTIC_WINDOW
    assert out_of_sync_ends < drift_ends

    await advance(hass, freezer, 10)
    await advance(hass, freezer, (out_of_sync_ends - dt_util.utcnow()).total_seconds() - 1)
    assert shown(hass).state == "out_of_sync"

    await advance(hass, freezer, 1)
    assert shown(hass).state == "drifted"

    await advance(hass, freezer, (drift_ends - dt_util.utcnow()).total_seconds() - 1)
    assert shown(hass).state == "drifted"

    await advance(hass, freezer, 1)
    assert shown(hass).state == "in_sync"


@pytest.mark.parametrize(*ems_entry())
async def test_expiry_timer_is_cancelled_when_the_entry_is_unloaded(hass, entry, coordinator, climate, boiler_data, freezer):
    unsubs = []

    def track(*args, **kwargs):
        unsub = MagicMock(side_effect=async_track_point_in_utc_time(*args, **kwargs))
        unsubs.append(unsub)

        return unsub

    with patch("custom_components.sat.sensor.async_track_point_in_utc_time", side_effect=track):
        value = await started(coordinator, climate, boiler_data)
        await step(climate, boiler_data, freezer, selflowtemp=value + 10)
        await step(climate, boiler_data, freezer, selflowtemp=value)
        assert shown(hass).state == "out_of_sync"
        assert unsubs and not unsubs[-1].called

        assert await hass.config_entries.async_unload(entry.entry_id)

    unsubs[-1].assert_called_once()
