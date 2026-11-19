"""PWM calls its change callback exactly once per update and reset, with the result already visible; the climate keeps the PWM sensors current and N frozen while PWM doesn't run."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.sat.climate import SatClimate
from custom_components.sat.const import (
    CONF_RADIATORS,
    SIGNAL_PWM_UPDATED,
    ModulationMode,
    PWMStatus,
)
from custom_components.sat.pwm import PWM
from tests.conftest import advance
from tests.const import IDLE, ems_entry, with_data
from tests.test_climate_loop_ems import INSIDE, OUTSIDE, RADIATOR
from tests.test_pwm_cycles import COLD, MILD, Clock, config, step

CURVE = SimpleNamespace(value=30.0, base_offset=20.0)
CYCLES = "sensor.pwm_cycles_per_hour"
STATUS = "sensor.pwm_status"
MODE = "sensor.pwm_mode"
DUTY = "sensor.pwm_duty_cycle_test"


@pytest.fixture
def clock():
    clock = Clock()
    with patch("custom_components.sat.pwm.monotonic", clock):
        yield clock


class Recorder:
    """Plain recording callback that snapshots the public PWM state at each call."""

    def __init__(self) -> None:
        self.pwm: PWM | None = None
        self.seen: list[tuple] = []

    def __call__(self) -> None:
        assert self.pwm is not None
        self.seen.append((self.pwm.status, self.pwm.mode, self.pwm.cycles_per_hour, self.pwm.duty_cycle))


def make(**overrides) -> tuple[PWM, Recorder]:
    recorder = Recorder()
    pwm = PWM(config(**overrides), CURVE, recorder)  # type: ignore[arg-type]
    recorder.pwm = pwm

    return pwm, recorder


def current(pwm: PWM) -> tuple:
    return pwm.status, pwm.mode, pwm.cycles_per_hour, pwm.duty_cycle


# The change callback

async def test_no_call_at_construction(clock):
    _, recorder = make()
    assert recorder.seen == []


UNRESTRICTED = {"allow_unrestricted": True, "minimum_setpoint": 10.0}

# Each case is the updates to run, then the status and N the last one leaves
OUTCOMES = {
    "pulse start": ([{"duty": 0.5, "outside_temperature": MILD}], PWMStatus.ON, 2),
    "no duty available": ([{"duty": 0.5, "flow": None}], PWMStatus.OFF, 4),
    "below the minimum duty": ([{"duty": 0.0}], PWMStatus.OFF, 4),
    "held start for hot water": ([{"duty": 0.5, "hot_water": True}], PWMStatus.OFF, 4),
    "running pulse": ([{"duty": 0.5}, {"duty": 0.5, "seconds": 10}], PWMStatus.ON, 4),
    "pulse end": ([{"duty": 0.5}, {"duty": 0.5, "seconds": 3600}], PWMStatus.OFF, 4),
    # The 100% period of 2 * 3600 / 4 s ends; the rollover latches the new N
    "rollover": ([{"duty": 1.0, **UNRESTRICTED}, {"duty": 1.0, "seconds": 1800, "outside_temperature": MILD, **UNRESTRICTED}], PWMStatus.ON, 2),
}


@pytest.mark.parametrize(("updates", "status", "cycles"), OUTCOMES.values(), ids=OUTCOMES.keys())
async def test_every_update_outcome_calls_on_change_once_and_the_callback_sees_the_final_state(clock, updates, status, cycles):
    pwm, recorder = make()

    for calls, update in enumerate(updates, start=1):
        await step(pwm, clock, **update)
        assert len(recorder.seen) == calls
        assert recorder.seen[-1] == current(pwm)

    assert (pwm.status, pwm.cycles_per_hour) == (status, cycles)


async def test_a_mode_change_is_visible_when_the_callback_is_called(clock):
    pwm, recorder = make()
    await step(pwm, clock, 0.5)
    recorder.seen.clear()

    await step(pwm, clock, 1.0, seconds=1, allow_unrestricted=True, minimum_setpoint=10.0)

    assert recorder.seen[0][1] == ModulationMode.UNRESTRICTED


async def test_a_reset_calls_once_and_shows_the_reset_state(clock):
    pwm, recorder = make()
    await step(pwm, clock, 1.0, allow_unrestricted=True, minimum_setpoint=10.0, outside_temperature=MILD)
    recorder.seen.clear()

    pwm.reset()

    assert recorder.seen == [(PWMStatus.OFF, ModulationMode.MINIMUM, 2, None)]


async def test_every_call_is_one_callback_over_a_sequence(clock):
    pwm, recorder = make()
    calls = 0
    for index in range(6):
        await step(pwm, clock, 0.5, seconds=30)
        calls += 1
        if index % 2:
            pwm.reset()
            calls += 1
        assert len(recorder.seen) == calls


# A reset keeps N

async def test_a_reset_does_not_change_n(clock):
    pwm, _ = make()
    await step(pwm, clock, 0.0, outside_temperature=MILD)
    assert pwm.cycles_per_hour == 2

    pwm.reset()

    assert pwm.cycles_per_hour == 2


# N is set at the start of an update, before the pulse decision

@pytest.mark.parametrize(("outside", "expected"), [(COLD, 4), (MILD, 2), (6.0, 3)])
async def test_the_pulse_starting_in_an_update_uses_the_n_of_that_update(clock, outside, expected):
    pwm, recorder = make()

    await step(pwm, clock, 0.5, outside_temperature=outside)

    assert pwm.status == PWMStatus.ON
    assert recorder.seen[-1][2] == expected
    # A mid duty fills the period 3600 / N
    assert sum(pwm.duty_cycle or ()) == 3600 // expected


# The PWM sensors and N at climate level

@pytest.fixture
def signals(hass: HomeAssistant, entry) -> list[PWMStatus]:
    received: list[PWMStatus] = []

    @callback
    def receive(status: PWMStatus) -> None:
        received.append(status)

    async_dispatcher_connect(hass, SIGNAL_PWM_UPDATED.format(entry.entry_id), receive)
    return received


async def heat(hass: HomeAssistant, climate: SatClimate, boiler_data) -> None:
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await hass.async_block_till_done()


async def loop(hass: HomeAssistant, climate: SatClimate) -> None:
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_all_pwm_sensors_update_during_a_heat_cycle(hass, entry, climate, boiler_data, signals):
    await heat(hass, climate, boiler_data)
    signals.clear()

    await loop(hass, climate)

    assert signals == [climate.pwm.status]
    assert hass.states.get(STATUS).state == climate.pwm.status.value == PWMStatus.ON.value
    assert hass.states.get(MODE).state == climate.pwm.mode.value
    assert hass.states.get(CYCLES).state == str(climate.pwm.cycles_per_hour)
    assert hass.states.get(DUTY).state not in ("unknown", "unavailable")
    # The outside sensor of the fixtures is mild
    assert climate.pwm.cycles_per_hour == 2


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_n_is_frozen_in_hvac_mode_off_and_follows_in_heat(hass, entry, climate, boiler_data):
    await boiler_data(**IDLE)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.OFF)
    await hass.async_block_till_done()
    assert hass.states.get(CYCLES).state == "4"

    # A mild outside temperature would give N=2
    hass.states.async_set(OUTSIDE, "20")
    await loop(hass, climate)
    assert climate.pwm.cycles_per_hour == 4
    assert hass.states.get(CYCLES).state == "4"

    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await loop(hass, climate)
    assert hass.states.get(CYCLES).state == "2"


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "loop_entry")
@pytest.mark.parametrize(*with_data({CONF_RADIATORS: [RADIATOR]}))
async def test_n_is_frozen_without_an_open_valve_and_follows_when_one_opens(hass, entry, climate, boiler_data):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    hass.states.async_set(RADIATOR, "heat", {"hvac_action": "idle", "hvac_modes": ["heat", "off"]})
    hass.states.async_set(OUTSIDE, "20")

    await loop(hass, climate)
    assert climate.valves_open is False
    assert climate.pwm.cycles_per_hour == 4
    assert hass.states.get(CYCLES).state == "4"

    hass.states.async_set(RADIATOR, "heat", {"hvac_action": "heating", "hvac_modes": ["heat", "off"]})
    await loop(hass, climate)
    assert hass.states.get(CYCLES).state == "2"


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "loop_entry", "no_input_grace")
@pytest.mark.parametrize(*ems_entry())
@pytest.mark.parametrize("missing", [INSIDE, OUTSIDE])
async def test_n_is_frozen_with_a_missing_temperature_and_follows_when_it_returns(hass, entry, climate, boiler_data, missing):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    hass.states.async_set(missing, "unavailable")
    # A mild outside temperature would give N=2
    if missing == INSIDE:
        hass.states.async_set(OUTSIDE, "20")
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    await loop(hass, climate)
    assert climate.pwm.cycles_per_hour == 4
    assert hass.states.get(CYCLES).state == "4"

    hass.states.async_set(missing, "20.9" if missing == INSIDE else "20")
    await loop(hass, climate)
    assert hass.states.get(CYCLES).state == "2"


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_the_cycles_sensor_shows_the_n_in_effect_at_the_last_update_after_pwm_stops_running(hass, entry, climate, boiler_data, freezer):
    # A pulse latches the cold N=4
    hass.states.async_set(OUTSIDE, "-5")
    await heat(hass, climate, boiler_data)
    await loop(hass, climate)
    assert hass.states.get(CYCLES).state == "4"

    # A mild update computes N=2, but the latch still holds
    hass.states.async_set(OUTSIDE, "20")
    await advance(hass, freezer, 11)
    await loop(hass, climate)
    assert hass.states.get(CYCLES).state == "4"

    # In OFF the latch expires (after 2 * 3600 / 4 s), but N is not refreshed from the outside temperature
    await climate.async_set_hvac_mode(HVACMode.OFF)
    await advance(hass, freezer, 3601)
    await loop(hass, climate)

    # Had PWM updated in OFF, the sensor would show the mild N=2 by now
    assert hass.states.get(CYCLES).state == "4"


