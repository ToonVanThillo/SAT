"""Flame tracking.

The first observation only seeds the flame state, and a burn already on at that moment is not counted as a cycle.
"""
import logging
from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import callback

from custom_components.sat.const import CONF_MQTT_TOPIC
from custom_components.sat.flame import Flame
from custom_components.sat.mqtt.ems import SatEmsMqttCoordinator
from tests.conftest import advance, boiler, current, flame_transitions
from tests.const import IDLE, ems_entry

LOGGER = "custom_components.sat.flame"


def observe(flame: Flame, freezer: FrozenDateTimeFactory, seconds: float, flame_active: bool) -> None:
    freezer.tick(timedelta(seconds=seconds))
    flame.update(boiler_state=boiler(flame_active))


@pytest.fixture
def flame(caplog) -> Flame:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
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
    assert flame_transitions(caplog) == ["Flame transition ON->OFF: burn started before the first observation, not counted"]


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
        "Flame transition OFF->ON",
        "Flame transition ON->OFF: duration=120.0s, cycles_last_hour=1.0, samples_4h=1",
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
        "Flame transition OFF->ON",
        "Flame transition ON->OFF: duration=90.0s, cycles_last_hour=1.0, samples_4h=1",
    ]


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
async def test_a_reload_restores_a_stored_burn_without_logging_a_transition(hass, entry, boiler_data, freezer, caplog):
    await boiler_data(**IDLE | {"burngas": "on"})
    await advance(hass, freezer, 10)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    caplog.clear()

    await hass.config_entries.async_reload(entry.entry_id)
    await advance(hass, freezer, 10)
    await boiler_data(**IDLE | {"burngas": "off"})
    await advance(hass, freezer, 10)

    assert flame_transitions(caplog) == ["Flame transition ON->OFF: burn started before the first observation, not counted"]
    assert current(hass, entry).flame_active is False
