"""Tests for the climate restore after a restart: the standard state and the user settings (restore data), nothing else."""
import json
from typing import Any

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.restore_state import ExtraStoredData

from custom_components.sat.climate import SatClimate, SatClimateRestoreData
from custom_components.sat.const import CLIMATE, CONF_ROOMS, CONF_WINDOW_SENSORS, DOMAIN
from tests.conftest import settle, tick
from tests.const import with_data

CLIMATE_ID = "climate.test"
ROOM = "climate.room"
OTHER_ROOM = "climate.other"
WINDOW = "binary_sensor.window"

pytestmark = pytest.mark.usefixtures("restore", "area_states", "mqtt_mock")

# Attributes an old state could carry; none but the standard ones may come back
OLD_ATTRIBUTES: dict[str, Any] = {
    "temperature": 22.5,
    "preset_mode": "away",
    "rooms": {"climate.stale": 5.0},
    "pre_custom_temperature": 11.0,
    "pre_activity_temperature": 12.0,
    "error": 9.0,
    "integral": 7.0,
    "derivative_raw": 3.0,
    "heating_curve": 44.0,
    "pulse_width_modulation_enabled": True,
    "setpoint": 55.0,
    "optimal_coefficient": 9.9,
    "coefficient_derivative": 1.5,
}
EXTRA = {"rooms": {ROOM: 20.0, OTHER_ROOM: 18.5}, "pre_custom_temperature": 19.0, "pre_activity_temperature": 17.5}
ROOMS_ENTRY = with_data({CONF_ROOMS: [ROOM, OTHER_ROOM]})


def old_state(state: str = "heat", **attributes) -> State:
    return State(CLIMATE_ID, state, attributes)


@pytest.fixture
def area_states(hass: HomeAssistant, request: pytest.FixtureRequest) -> None:
    """Set the area climate states before the entry is set up; param maps entity id to (state, attributes)."""
    for entity_id, (state, attributes) in getattr(request, "param", {
        ROOM: ("heat", {"temperature": 21.0, "hvac_modes": ["heat", "off"]}),
        OTHER_ROOM: ("heat", {"temperature": 16.0, "hvac_modes": ["heat", "off"]}),
    }).items():
        hass.states.async_set(entity_id, state, attributes)


async def set_window(hass: HomeAssistant, state: str) -> None:
    hass.states.async_set(WINDOW, state)
    await settle()


def attributes_of(hass: HomeAssistant, climate: SatClimate) -> dict:
    state = hass.states.get(climate.entity_id)
    assert state is not None
    return dict(state.attributes)


def settings_of(climate: SatClimate) -> SatClimateRestoreData:
    """Return the user settings the climate holds, as it would store them."""
    return climate.extra_restore_state_data


def startup_log(caplog) -> str:
    """Return the messages logged while the entry was set up (fixture phase)."""
    return "\n".join(r.getMessage() for r in caplog.get_records("setup"))


# SatClimateRestoreData

def data(**overrides: Any) -> SatClimateRestoreData:
    defaults: dict[str, Any] = {"rooms": {ROOM: 20.0}, "pre_custom_temperature": 19.0, "pre_activity_temperature": 17.5}
    return SatClimateRestoreData(**defaults | overrides)


@pytest.mark.parametrize("restore", [None], indirect=True)
class TestRestoreData:
    def test_is_extra_stored_data_and_frozen(self):
        restore_data = data()
        assert isinstance(restore_data, ExtraStoredData)
        with pytest.raises(AttributeError):
            restore_data.rooms = {}  # type: ignore[misc]

    def test_is_keyword_only(self):
        with pytest.raises(TypeError):
            SatClimateRestoreData({ROOM: 20.0}, 1.0, 2.0)  # type: ignore[misc]

    def test_as_dict_is_json_serializable_and_copies_rooms(self):
        rooms = {ROOM: 20.0}
        as_dict = data(rooms=rooms).as_dict()

        assert as_dict == {"rooms": {ROOM: 20.0}, "pre_custom_temperature": 19.0, "pre_activity_temperature": 17.5}
        assert json.loads(json.dumps(as_dict)) == as_dict
        assert as_dict["rooms"] is not rooms

    def test_as_dict_keeps_none(self):
        assert data(rooms=None, pre_custom_temperature=None, pre_activity_temperature=None).as_dict() == {
            "rooms": None, "pre_custom_temperature": None, "pre_activity_temperature": None}

    @pytest.mark.parametrize("original", [
        data(),
        data(rooms=None),
        data(rooms={}),
        data(pre_custom_temperature=None),
        data(pre_activity_temperature=None, rooms={ROOM: 20.0, OTHER_ROOM: 18.5}),
    ])
    def test_round_trip(self, original):
        assert SatClimateRestoreData.from_dict(original.as_dict()) == original

    def test_from_dict_converts_types(self):
        assert SatClimateRestoreData.from_dict({"rooms": {ROOM: "20.5", 5: 18}, "pre_custom_temperature": "19", "pre_activity_temperature": 17}) == data(
            rooms={ROOM: 20.5, "5": 18.0}, pre_custom_temperature=19.0, pre_activity_temperature=17.0)

    @pytest.mark.parametrize("missing", ["rooms", "pre_custom_temperature", "pre_activity_temperature"])
    def test_from_dict_returns_none_when_a_key_is_missing(self, missing):
        assert SatClimateRestoreData.from_dict({k: v for k, v in EXTRA.items() if k != missing}) is None

    @pytest.mark.parametrize("broken", [
        {"rooms": ["a"]},
        {"rooms": 5},
        {"rooms": "text"},
        {"rooms": {ROOM: "warm"}},
        {"rooms": {ROOM: None}},
        {"pre_custom_temperature": "warm"},
        {"pre_activity_temperature": [1]},
        {"pre_custom_temperature": {}},
    ])
    def test_from_dict_returns_none_on_a_wrong_type(self, broken):
        assert SatClimateRestoreData.from_dict(EXTRA | broken) is None

    def test_from_dict_returns_none_for_empty_data(self):
        assert SatClimateRestoreData.from_dict({}) is None


# Restore on startup

@pytest.mark.parametrize(*ROOMS_ENTRY)
@pytest.mark.parametrize("restore", [(old_state(**OLD_ATTRIBUTES), EXTRA)], indirect=True)
async def test_the_standard_state_and_user_settings_are_restored(hass, entry, climate, restore):
    attributes = attributes_of(hass, climate)

    assert hass.states.get(climate.entity_id).state == "heat"
    assert attributes["temperature"] == 22.5
    assert attributes["preset_mode"] == "away"
    settings = settings_of(climate)
    assert settings.rooms == {ROOM: 20.0, OTHER_ROOM: 18.5}
    assert settings.pre_custom_temperature == 19.0
    assert settings.pre_activity_temperature == 17.5


@pytest.mark.parametrize(*ROOMS_ENTRY)
@pytest.mark.parametrize("restore", [(old_state(**OLD_ATTRIBUTES), EXTRA)], indirect=True)
async def test_control_state_is_not_restored(hass, entry, climate, restore):
    assert climate.pid.integral == 0
    assert climate.pid.raw_derivative == 0
    assert climate.heating_curve.optimal_coefficient != 9.9
    assert climate.heating_curve.coefficient_derivative != 1.5
    assert climate.heating_curve.value != 44.0
    assert climate.max_error.value != 9.0
    assert climate.setpoint != 55.0
    assert climate.pwm.enabled is False


@pytest.mark.parametrize(*ROOMS_ENTRY)
@pytest.mark.parametrize("restore", [(old_state(**OLD_ATTRIBUTES), None)], indirect=True)
async def test_control_state_matches_a_fresh_start_without_extra_data(hass, entry, climate, restore):
    assert (climate.pid.integral, climate.pid.raw_derivative) == (0, 0)
    assert climate.setpoint != 55.0
    assert climate.pwm.enabled is False


@pytest.mark.parametrize(*ROOMS_ENTRY)
@pytest.mark.parametrize("restore", [(old_state(**OLD_ATTRIBUTES), EXTRA | {"pre_custom_temperature": None, "pre_activity_temperature": None})], indirect=True)
async def test_stored_none_temperatures_stay_none(hass, entry, climate, restore):
    settings = settings_of(climate)

    assert settings.pre_custom_temperature is None
    assert settings.pre_activity_temperature is None
    assert settings.rooms == {ROOM: 20.0, OTHER_ROOM: 18.5}


@pytest.mark.parametrize(*ROOMS_ENTRY)
@pytest.mark.parametrize("restore", [
    (old_state(**OLD_ATTRIBUTES), None),
    (old_state(**OLD_ATTRIBUTES), {}),
    (old_state(**OLD_ATTRIBUTES), {"rooms": {ROOM: 20.0}}),
    (old_state(**OLD_ATTRIBUTES), EXTRA | {"pre_custom_temperature": "warm"}),
    (old_state(**OLD_ATTRIBUTES), EXTRA | {"rooms": 5}),
], ids=["no-extra", "empty-extra", "missing-keys", "bad-temperature", "bad-rooms"], indirect=True)
async def test_missing_or_malformed_extra_data_ignores_the_old_attributes(hass, entry, climate, restore):
    settings = settings_of(climate)

    assert settings.pre_custom_temperature is None
    assert settings.pre_activity_temperature is None
    # Rooms are rebuilt from the area climates, never from the old attributes
    assert settings.rooms == {ROOM: 21.0, OTHER_ROOM: 16.0}
    assert attributes_of(hass, climate)["temperature"] == 22.5


@pytest.mark.parametrize(*ROOMS_ENTRY)
@pytest.mark.parametrize("restore", [
    (old_state(**OLD_ATTRIBUTES), EXTRA | {"rooms": None}),
    (old_state(**OLD_ATTRIBUTES), EXTRA | {"rooms": {}}),
], ids=["none", "empty"], indirect=True)
async def test_none_or_empty_stored_rooms_are_rebuilt(hass, entry, climate, restore):
    settings = settings_of(climate)

    assert settings.rooms == {ROOM: 21.0, OTHER_ROOM: 16.0}
    assert settings.pre_custom_temperature == 19.0
    assert settings.pre_activity_temperature == 17.5


@pytest.mark.parametrize(*ROOMS_ENTRY)
@pytest.mark.parametrize("area_states, expected_rooms", [
    ({
        ROOM: ("unavailable", {"temperature": 21.0}),
        OTHER_ROOM: ("heat", {"temperature": 16.0, "hvac_modes": ["heat", "off"]}),
    }, {OTHER_ROOM: 16.0}),
    ({
        ROOM: ("unknown", {"temperature": 21.0}),
        OTHER_ROOM: ("heat", {}),
    }, {}),
], ids=["unavailable", "unknown-and-no-temperature"], indirect=["area_states"])
@pytest.mark.parametrize("restore", [None], indirect=True)
async def test_rebuilt_rooms_skip_unavailable_areas_and_areas_without_a_target(hass, entry, climate, restore, area_states, expected_rooms):
    assert settings_of(climate).rooms == expected_rooms


@pytest.mark.parametrize(*ROOMS_ENTRY)
@pytest.mark.parametrize("restore", [None], indirect=True)
async def test_rooms_are_built_from_the_area_climates_without_any_stored_data(hass, entry, climate, restore):
    assert settings_of(climate).rooms == {ROOM: 21.0, OTHER_ROOM: 16.0}


# Old state handling (unchanged behaviour)

@pytest.mark.parametrize(*ROOMS_ENTRY)
@pytest.mark.parametrize("restore", [(old_state("off", preset_mode="away", temperature=19.0), None)], indirect=True)
async def test_hvac_mode_target_and_preset_come_from_the_old_state(hass, entry, climate, restore):
    assert climate.hvac_mode == HVACMode.OFF
    assert climate.target_temperature == 19.0
    assert climate.preset_mode == "away"


@pytest.mark.parametrize(*ROOMS_ENTRY)
@pytest.mark.parametrize("restore", [(old_state("heat"), None)], indirect=True)
async def test_a_missing_target_temperature_falls_back_to_the_minimum(hass, entry, climate, restore, caplog):
    assert climate.target_temperature == climate.min_temp
    assert climate.hvac_mode == HVACMode.HEAT
    assert f"Undefined target temperature, falling back to {climate.min_temp}" in startup_log(caplog)


@pytest.mark.parametrize(*ROOMS_ENTRY)
@pytest.mark.parametrize("restore", [None], indirect=True)
async def test_without_an_old_state_the_defaults_apply(hass, entry, climate, restore, caplog):
    assert climate.target_temperature == climate.min_temp
    assert climate.hvac_mode == HVACMode.OFF
    assert f"No previously saved temperature, setting to {climate.min_temp}" in startup_log(caplog)
    assert "Undefined target temperature" not in startup_log(caplog)
    assert settings_of(climate).pre_custom_temperature is None


@pytest.mark.parametrize(*ROOMS_ENTRY)
@pytest.mark.parametrize("restore", [(old_state("heat", temperature=20.0), None)], indirect=True)
async def test_an_old_state_logs_no_missing_temperature_warning(hass, entry, climate, restore, caplog):
    assert climate.target_temperature == 20.0
    assert "Undefined target temperature" not in startup_log(caplog)


# extra_restore_state_data and the round trip

@pytest.mark.parametrize(*ROOMS_ENTRY)
@pytest.mark.parametrize("restore", [(old_state(**OLD_ATTRIBUTES), EXTRA)], indirect=True)
async def test_extra_restore_state_data_reflects_the_current_settings(hass, entry, climate, restore):
    assert climate.extra_restore_state_data == SatClimateRestoreData(
        rooms={ROOM: 20.0, OTHER_ROOM: 18.5}, pre_custom_temperature=19.0, pre_activity_temperature=17.5)


@pytest.mark.parametrize(*ROOMS_ENTRY)
@pytest.mark.parametrize("restore", [None], indirect=True)
async def test_extra_restore_state_data_without_stored_settings(hass, entry, climate, restore):
    assert climate.extra_restore_state_data == SatClimateRestoreData(
        rooms={ROOM: 21.0, OTHER_ROOM: 16.0}, pre_custom_temperature=None, pre_activity_temperature=None)


@pytest.mark.usefixtures("window_sensor_registered", "base_components")
@pytest.mark.parametrize(*with_data({CONF_ROOMS: [ROOM, OTHER_ROOM]}, {CONF_WINDOW_SENSORS: [WINDOW]}))
@pytest.mark.parametrize("restore", [None], indirect=True)
async def test_settings_survive_a_restart(hass, freezer, entry, climate, restore):
    # The user settings are set through public paths only; the reload stores the entity's real restore data
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_set_target_temperature(21.0)

    # An open window records the target to return to after it
    await set_window(hass, STATE_ON)
    await tick(hass, freezer, 20)
    assert climate.preset_mode == "activity"
    await set_window(hass, STATE_OFF)
    assert climate.target_temperature == 21.0

    # Selecting a preset from "none" records the target to return to after it
    await climate.async_set_temperature(temperature=20.5)
    await climate.async_set_preset_mode("home")
    assert (climate.target_temperature, climate.preset_mode) == (18.0, "home")

    # During the home preset, area targets are followed
    hass.states.async_set(ROOM, "heat", {"temperature": 23.0, "hvac_modes": ["heat", "off"]})
    hass.states.async_set(OTHER_ROOM, "heat", {"temperature": 15.0, "hvac_modes": ["heat", "off"]})
    await settle()
    await climate.async_set_preset_mode("comfort")
    assert climate.extra_restore_state_data == SatClimateRestoreData(
        rooms={ROOM: 23.0, OTHER_ROOM: 15.0}, pre_custom_temperature=20.5, pre_activity_temperature=21.0)

    # Area targets that changed afterwards must not win over the stored rooms
    hass.states.async_set(ROOM, "heat", {"temperature": 12.0, "hvac_modes": ["heat", "off"]})
    hass.states.async_set(OTHER_ROOM, "heat", {"temperature": 12.0, "hvac_modes": ["heat", "off"]})
    await settle()

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restarted = hass.data[DOMAIN][entry.entry_id][CLIMATE]

    assert restarted is not climate
    assert restarted.extra_restore_state_data == SatClimateRestoreData(
        rooms={ROOM: 23.0, OTHER_ROOM: 15.0}, pre_custom_temperature=20.5, pre_activity_temperature=21.0)
    assert restarted.preset_mode == "comfort"
    assert restarted.target_temperature == 20.0
    assert restarted.hvac_mode == HVACMode.HEAT
