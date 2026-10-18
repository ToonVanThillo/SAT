"""Characterization tests for the OpenTherm gateway MQTT coordinator."""
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.sat.const import CONF_MANUFACTURER, CONF_MQTT_TOPIC
from custom_components.sat.coordinator import DeviceState
from custom_components.sat.mqtt.opentherm import SatOpenThermMqttCoordinator
from tests.const import otgw_entry
from tests.test_climate_loop import run_loop

TOPIC = "otgw"
COMMAND_TOPIC = "otgw/set/gw1/command"


def build(hass: HomeAssistant, manufacturer: str | None = None) -> SatOpenThermMqttCoordinator:
    config = {CONF_MQTT_TOPIC: TOPIC}
    if manufacturer is not None:
        config[CONF_MANUFACTURER] = manufacturer

    return SatOpenThermMqttCoordinator(hass, "gw1", config)


async def feed(hass: HomeAssistant, key: str, payload: str) -> None:
    async_fire_mqtt_message(hass, f"{TOPIC}/value/gw1/{key}", payload)
    await hass.async_block_till_done()


async def test_reads_one_topic_per_key(hass, otgw):
    await feed(hass, "centralheating", "ON")
    await feed(hass, "flame", "ON")
    await feed(hass, "domestichotwater", "OFF")
    await feed(hass, "TSet", "55.5")
    await feed(hass, "TdhwSet", "50.0")
    await feed(hass, "Tboiler", "48.3")
    await feed(hass, "Tret", "41.0")
    await feed(hass, "RelModLevel", "37.0")
    await feed(hass, "MaxRelModLevelSetting", "80.0")
    await feed(hass, "MaxCapacityMinModLevel_hb_u8", "24")
    await feed(hass, "MaxCapacityMinModLevel_lb_u8", "20")
    await feed(hass, "slave_memberid_code", "27")

    assert otgw.device_active is True
    assert otgw.flame_active is True
    assert otgw.hot_water_active is False
    assert otgw.setpoint == 55.5
    assert otgw.hot_water_setpoint == 50.0
    assert otgw.boiler_temperature == 48.3
    assert otgw.return_temperature == 41.0
    assert otgw.relative_modulation_value == 37.0
    assert otgw.maximum_relative_modulation_value == 80.0
    assert otgw.boiler_capacity == 24
    assert otgw.minimum_relative_modulation_value == 20
    assert otgw.member_id == 27
    assert otgw.data["TSet"] == "55.5"


async def test_defaults_without_data(otgw):
    assert otgw.device_active is False
    assert otgw.setpoint is None
    assert otgw.boiler_temperature is None
    assert otgw.member_id is None
    assert otgw.minimum_hot_water_setpoint == 30
    assert otgw.maximum_hot_water_setpoint == 60


async def test_legacy_minimum_modulation_key(hass, otgw):
    await feed(hass, "MaxCapacityMinModLevell_lb_u8", "15")

    assert otgw.minimum_relative_modulation_value == 15


async def test_device_active_only_for_exact_on(hass, otgw):
    await feed(hass, "centralheating", "on")

    assert otgw.device_active is False


async def test_non_numeric_payload_raises_on_access(hass, otgw):
    await feed(hass, "TSet", "unavailable")

    # current behaviour, known bug: bare float() raises at property access instead of returning None
    with pytest.raises(ValueError):
        _ = otgw.setpoint


async def test_boot_default(hass, mqtt_mock, published, no_publish_delay):
    await build(hass).boot()

    assert published() == [(COMMAND_TOPIC, "PM=3", 1, False), (COMMAND_TOPIC, "PM=15", 1, False), (COMMAND_TOPIC, "PM=48", 1, False)]


@pytest.mark.parametrize("manufacturer", ["Ideal", "Intergas", "Nefit"])
async def test_boot_sends_minimum_interval_for_some_manufacturers(hass, mqtt_mock, published, no_publish_delay, manufacturer):
    await build(hass, manufacturer).boot()

    assert [call[1] for call in published()] == ["PM=3", "PM=15", "PM=48", "MI=500"]


async def test_control_setpoint_sends_cs_then_pm(otgw, published):
    await otgw.async_set_control_setpoint(55.5)

    assert published() == [(COMMAND_TOPIC, "CS=55.5", 1, False), (COMMAND_TOPIC, "PM=25", 1, False)]


@pytest.mark.parametrize("method, value, payload", [
    ("async_set_control_hot_water_setpoint", 50, "SW=50"),
    ("async_set_control_thermostat_setpoint", 20.5, "TC=20.5"),
    ("async_set_control_max_setpoint", 70.0, "SH=70.0"),
])
async def test_single_writes(otgw, published, method, value, payload):
    await getattr(otgw, method)(value)

    assert published() == [(COMMAND_TOPIC, payload, 1, False)]


@pytest.mark.parametrize("state, payload", [(DeviceState.ON, "CH=1"), (DeviceState.OFF, "CH=0")])
async def test_heater_state(otgw, published, state, payload):
    await otgw.async_set_heater_state(state)

    assert published() == [(COMMAND_TOPIC, payload, 1, False)]


async def test_max_relative_modulation_default(otgw, published):
    await otgw.async_set_control_max_relative_modulation(60)

    assert published() == [(COMMAND_TOPIC, "MM=60", 1, False)]


@pytest.mark.parametrize("value, tp", [(60, 60), (100, 80)])
async def test_max_relative_modulation_immergas(hass, mqtt_mock, published, no_publish_delay, value, tp):
    await build(hass, "Immergas").async_set_control_max_relative_modulation(value)

    assert published() == [(COMMAND_TOPIC, f"TP=11:12={tp}", 1, False), (COMMAND_TOPIC, f"MM={value}", 1, False)]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*otgw_entry())
async def test_publish_failure_in_the_loop_is_swallowed(entry, climate, mqtt_mock, published, caplog):
    mqtt_mock.async_publish.side_effect = HomeAssistantError("broker down")

    sent = await run_loop(climate, mqtt_mock, published)

    assert any(c[1].startswith("CS=") for c in sent)
    assert "Failed to publish MQTT command" in caplog.text
    assert "Failed to write" not in caplog.text
