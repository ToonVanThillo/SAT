"""Flame tracking.

The first observation only seeds the flame state, and a burn already on at that moment is not counted as a cycle.
"""
import logging
from dataclasses import replace
from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import callback

from custom_components.sat.const import (
    CONF_MQTT_TOPIC,
    FlameStatus,
    ModulationMode,
    PWMStatus,
)
from custom_components.sat.flame import Flame, HotWaterSignal
from custom_components.sat.mqtt.ems import SatEmsMqttCoordinator
from custom_components.sat.pwm import PWMState
from tests.conftest import TIMELINE, advance, boiler, current, flame_transitions
from tests.const import IDLE, ems_entry

PULSE = PWMState(status=PWMStatus.ON, duty_cycle=(450, 450), mode=ModulationMode.MINIMUM)
FULL = PWMState(status=PWMStatus.ON, duty_cycle=(1800, 0), mode=ModulationMode.MINIMUM)


def observe(flame: Flame, freezer: FrozenDateTimeFactory, seconds: float, flame_active: bool) -> None:
    freezer.tick(timedelta(seconds=seconds))
    flame.update(boiler_state=boiler(flame_active))


@pytest.fixture
def flame(caplog) -> Flame:
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    return Flame()


def test_flame_on_at_the_first_observation_logs_no_transition(flame, freezer, caplog):
    observe(flame, freezer, 0, True)

    assert flame.is_active
    assert flame.on_since is not None
    assert flame_transitions(caplog) == []


def test_flame_off_at_the_first_observation_logs_no_transition(flame, freezer, caplog):
    observe(flame, freezer, 0, False)

    assert flame.is_inactive
    assert flame.off_since is not None
    assert flame_transitions(caplog) == []


def test_a_burn_on_at_the_first_observation_adds_no_cycle(flame, freezer, caplog):
    observe(flame, freezer, 0, True)
    observe(flame, freezer, 600, False)

    assert flame.cycles_last_hour == 0
    assert flame.sample_count_4h == 0
    assert flame.median_on_duration_seconds_4h is None
    assert flame.last_cycle_duration_seconds is None
    assert flame_transitions(caplog) == ["flame off, heating (burn started before the first observation, not counted)"]


def test_cycles_after_a_partial_burn_count_normally(flame, freezer, caplog):
    observe(flame, freezer, 0, True)
    observe(flame, freezer, 600, False)
    caplog.clear()

    observe(flame, freezer, 60, True)
    observe(flame, freezer, 120, False)

    assert flame.cycles_last_hour == 1
    assert flame.sample_count_4h == 1
    assert flame.last_cycle_duration_seconds == 120
    assert flame_transitions(caplog) == [
        "flame on",
        "flame off after 120.0s, heating (cycles_last_hour=1.0, samples_4h=1)",
    ]

    # Completing a full cycle starts the average on time with the next burn
    observe(flame, freezer, 60, True)
    observe(flame, freezer, 30, True)
    assert flame.average_on_time_seconds == 30


def test_repeated_on_observations_do_not_count_a_partial_burn(flame, freezer):
    for _ in range(3):
        observe(flame, freezer, 20, True)
    observe(flame, freezer, 20, False)

    assert flame.cycles_last_hour == 0
    assert flame.last_cycle_duration_seconds is None


def test_no_average_on_time_after_only_a_partial_burn(flame, freezer):
    observe(flame, freezer, 0, True)
    observe(flame, freezer, 600, False)

    observe(flame, freezer, 60, True)
    observe(flame, freezer, 30, True)

    assert flame.average_on_time_seconds is None


def test_a_cycle_after_flame_off_at_the_first_observation_counts(flame, freezer, caplog):
    observe(flame, freezer, 0, False)
    observe(flame, freezer, 60, True)
    observe(flame, freezer, 90, False)

    assert flame.cycles_last_hour == 1
    assert flame.last_cycle_duration_seconds == 90
    assert flame_transitions(caplog) == [
        "flame on",
        "flame off after 90.0s, heating (cycles_last_hour=1.0, samples_4h=1)",
    ]


# Burns classified by the signals given to `update`, as for backends without updates on arrival

def test_a_burn_with_a_hot_water_signal_is_hot_water_until_the_flame_goes_off(flame, freezer, caplog):
    observe(flame, freezer, 0, False)
    freezer.tick(timedelta(seconds=10))
    flame.update(boiler_state=boiler(True), hot_water_signals={HotWaterSignal.FLAG})
    observe(flame, freezer, 20, True)

    assert flame.hot_water_burn

    observe(flame, freezer, 10, False)

    assert not flame.hot_water_burn
    assert flame.cycles_last_hour == 0
    assert flame.sample_count_4h == 0
    assert flame.duty_ratio_last_15m == 0
    assert flame_transitions(caplog) == ["flame on", "flame off after 30.0s, hot water (flag), not counted"]


def test_a_partial_burn_logs_its_kind(flame, freezer, caplog):
    flame.update(boiler_state=boiler(True), hot_water_signals={HotWaterSignal.BURNER_POWER})
    observe(flame, freezer, 60, False)

    assert flame_transitions(caplog) == ["flame off, hot water (burner_power) (burn started before the first observation, not counted)"]


# Health under PWM: the PWM rules judge pulses below 100% duty, the continuous rules judge 100% periods

def burn_history(flame: Flame, freezer: FrozenDateTimeFactory, pwm_state: PWMState | None, burns: int, off_seconds: float, on_seconds: float) -> None:
    """Run `burns` heating burns of `on_seconds`, each after `off_seconds` of flame off, reporting `pwm_state` throughout."""
    flame.update(boiler_state=boiler(False), pwm_state=pwm_state)
    for _ in range(burns):
        freezer.tick(timedelta(seconds=off_seconds))
        flame.update(boiler_state=boiler(True), pwm_state=pwm_state)
        freezer.tick(timedelta(seconds=on_seconds))
        flame.update(boiler_state=boiler(False), pwm_state=pwm_state)


# (burns, off seconds before each 20 s burn, status under the continuous rules): 8 in the last hour, or 3
SHORT_BURNS = [
    pytest.param(8, 400, FlameStatus.SHORT_CYCLING, id="more_than_3_per_hour"),
    pytest.param(3, 1500, FlameStatus.HEALTHY, id="3_per_hour"),
]

# (burns, off seconds, on seconds): short and long burns, few and many per hour
HISTORIES = [
    pytest.param(3, 1500, 20, id="few_short"),
    pytest.param(8, 400, 20, id="many_short"),
    pytest.param(3, 600, 450, id="few_long"),
    pytest.param(10, 250, 100, id="many_medium"),
    pytest.param(3, 100, 1000, id="few_very_long"),
]


@pytest.mark.parametrize("mode", list(ModulationMode))
@pytest.mark.parametrize(("burns", "off_seconds", "expected"), SHORT_BURNS)
def test_a_100_percent_period_is_judged_by_the_continuous_rules(flame, freezer, mode, burns, off_seconds, expected):
    burn_history(flame, freezer, replace(FULL, mode=mode), burns, off_seconds, 20)

    assert flame.health_status == expected


@pytest.mark.parametrize(("burns", "off_seconds"), [pytest.param(8, 400, id="more_than_3_per_hour"), pytest.param(3, 1500, id="3_per_hour")])
def test_a_pulse_below_100_percent_is_judged_by_the_pwm_rules(flame, freezer, burns, off_seconds):
    burn_history(flame, freezer, PULSE, burns, off_seconds, 20)

    assert flame.health_status == FlameStatus.PWM_SHORT


@pytest.mark.parametrize("mode", list(ModulationMode))
@pytest.mark.parametrize(("burns", "off_seconds", "on_seconds"), HISTORIES)
def test_a_100_percent_period_is_judged_like_no_pwm_state(flame, freezer, mode, burns, off_seconds, on_seconds):
    burn_history(flame, freezer, replace(FULL, mode=mode), burns, off_seconds, on_seconds)
    other = Flame()
    burn_history(other, freezer, None, burns, off_seconds, on_seconds)

    assert flame.health_status == other.health_status


@pytest.mark.parametrize(("burns", "off_seconds", "on_seconds"), HISTORIES)
def test_status_off_after_pulses_is_judged_by_the_continuous_rules(flame, freezer, burns, off_seconds, on_seconds):
    burn_history(flame, freezer, replace(PULSE, status=PWMStatus.OFF), burns, off_seconds, on_seconds)
    other = Flame()
    burn_history(other, freezer, None, burns, off_seconds, on_seconds)

    assert flame.health_status != FlameStatus.PWM_SHORT
    assert flame.health_status == other.health_status


def test_switching_between_a_pulse_and_a_100_percent_period_changes_the_rule_set(flame, freezer):
    burn_history(flame, freezer, PULSE, 3, 1500, 20)
    assert flame.health_status == FlameStatus.PWM_SHORT

    flame.update(boiler_state=boiler(False), pwm_state=FULL)
    assert flame.health_status == FlameStatus.HEALTHY

    flame.update(boiler_state=boiler(False), pwm_state=PULSE)
    assert flame.health_status == FlameStatus.PWM_SHORT


def test_no_pwm_state_keeps_the_previous_100_percent_judgement(flame, freezer):
    burn_history(flame, freezer, FULL, 3, 1500, 20)
    assert flame.health_status == FlameStatus.HEALTHY

    flame.update(boiler_state=boiler(False))

    assert flame.health_status == FlameStatus.HEALTHY


# Through the coordinator

async def test_the_flame_is_updated_before_listeners_added_before_the_start(hass, mqtt_mock, no_publish_delay, boiler_data, freezer):
    coordinator = SatEmsMqttCoordinator(hass, "ems", {CONF_MQTT_TOPIC: "ems-esp"}, config_entry=None)
    seen: list[bool] = []

    # The climate's own listener is registered before it starts the coordinator
    coordinator.async_add_listener(callback(lambda: seen.append(coordinator.flame.is_active)))
    await coordinator.async_added_to_hass()

    await boiler_data(**IDLE | {"burngas": "on"})
    await advance(hass, freezer, 10)

    assert seen == [True]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_a_burn_running_across_a_reload_is_partial(hass, entry, boiler_data, freezer, caplog):
    await boiler_data(**IDLE | {"burngas": "on"})
    await advance(hass, freezer, 10)
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    caplog.clear()

    # The stored flame doesn't seed the flame after the reload; the first live value does
    await hass.config_entries.async_reload(entry.entry_id)
    await advance(hass, freezer, 10)
    await boiler_data(**IDLE | {"burngas": "on"})
    await advance(hass, freezer, 60)
    await boiler_data(**IDLE | {"burngas": "off"})
    await advance(hass, freezer, 10)

    assert flame_transitions(caplog) == ["flame off, heating (burn started before the first observation, not counted)"]
    assert current(hass, entry).flame_active is False
    assert current(hass, entry).flame.cycles_last_hour == 0


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_a_stored_burn_that_ended_during_a_reload_logs_nothing(hass, entry, boiler_data, freezer, caplog):
    await boiler_data(**IDLE | {"burngas": "on"})
    await advance(hass, freezer, 10)
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    caplog.clear()

    await hass.config_entries.async_reload(entry.entry_id)
    await advance(hass, freezer, 10)
    await boiler_data(**IDLE | {"burngas": "off"})
    await advance(hass, freezer, 10)

    assert flame_transitions(caplog) == []
    assert current(hass, entry).flame_active is False
