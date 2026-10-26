"""Timeline logger at the MQTT boundary, from the spec: boiler signal changes, ordering, edges, cycle lines and the absence of per-cycle noise."""
import logging
import re
from dataclasses import dataclass
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.sat import timeline as timeline_module
from custom_components.sat.channels import DesiredState, Quantity, WriteKind
from custom_components.sat.const import (
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    CONF_MINIMUM_SETPOINT,
    BoilerStatus,
)
from custom_components.sat.coordinator import DeviceState
from custom_components.sat.fake import SatFakeCoordinator
from custom_components.sat.mqtt.ems import SatEmsMqttCoordinator
from tests.conftest import TIMELINE, advance
from tests.const import IDLE, ems_entry, otgw_entry, with_data
from tests.test_climate_loop import run_loop
from tests.test_mqtt_ems import start

BRANCHES = {"manual", "low-duty", "low", "mid", "high", "max"}


def timeline(caplog, level=logging.INFO, phase="call") -> list[str]:
    return [r.getMessage() for r in caplog.get_records(phase) if r.name == TIMELINE and r.levelno == level]


def cycle_lines(caplog) -> list[str]:
    return [m for m in timeline(caplog, logging.DEBUG) if m.startswith("cycle ")]


def segments(line: str) -> list[str]:
    return line.split(" | ")


# Boiler signal changes (EMS coordinator without a climate)

async def test_boiler_changes_are_logged_on_arrival_only_when_changed(hass, mqtt_mock, no_publish_delay, boiler_data, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    await start(hass, {})
    caplog.clear()

    await boiler_data(heatingactive="off", tapwateractive="on", selflowtemp=40)
    assert sorted(timeline(caplog)) == sorted(["boiler heatingactive ? -> off", "boiler tapwateractive ? -> on", "boiler selflowtemp ? -> 40"])

    caplog.clear()
    await boiler_data(heatingactive="off", tapwateractive="on", selflowtemp=40)
    assert timeline(caplog) == []

    await boiler_data(heatingactive="on", selflowtemp=45)
    assert sorted(timeline(caplog)) == sorted(["boiler heatingactive off -> on", "boiler selflowtemp 40 -> 45"])


@pytest.mark.parametrize("key", ["heatingpump", "heatingactive", "tapwateractive", "burngas", "selflowtemp", "selburnpow", "servicecode", "heatingoff"])
async def test_every_traced_key_is_logged(hass, mqtt_mock, no_publish_delay, boiler_data, caplog, key):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    await start(hass, {})
    caplog.clear()

    await boiler_data(**{key: "on" if key in {"heatingpump", "heatingactive", "tapwateractive", "burngas", "heatingoff"} else 5})

    assert len(timeline(caplog)) == 1
    assert timeline(caplog)[0].startswith(f"boiler {key} ? -> ")


async def test_untraced_keys_are_never_logged(hass, mqtt_mock, no_publish_delay, boiler_data, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    await start(hass, {})
    caplog.clear()

    await boiler_data(curflowtemp=48.5, rettemp=41.2, curburnpow=37)
    await boiler_data(curflowtemp=50.5, rettemp=42.2, curburnpow=38)

    assert timeline(caplog) == []


async def test_restored_values_are_not_logged_and_the_first_live_value_is_shown_as_unknown(hass, mqtt_mock, no_publish_delay, boiler_data, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    coordinator = await start(hass, {})
    caplog.clear()

    coordinator.async_set_updated_data({"heatingactive": "on", "selflowtemp": 40}, restored=True)
    assert timeline(caplog) == []

    # The first live value is logged even when it equals the restored one
    await boiler_data(heatingactive="on", selflowtemp=40)
    assert sorted(timeline(caplog)) == ["boiler heatingactive ? -> on", "boiler selflowtemp ? -> 40"]

    caplog.clear()
    await boiler_data(heatingactive="off")
    assert timeline(caplog) == ["boiler heatingactive on -> off"]


async def test_boiler_change_is_logged_before_the_debounced_notify(hass, mqtt_mock, no_publish_delay, caplog, freezer):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    coordinator = await start(hass, {})
    events: list[str] = []
    coordinator.async_add_listener(lambda: events.append("notify"))

    class Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.name == TIMELINE:
                events.append(record.getMessage())

    handler = Handler()
    logging.getLogger(TIMELINE).addHandler(handler)
    try:
        async_fire_mqtt_message(hass, "ems-esp/boiler_data", '{"burngas": "on"}')
        await hass.async_block_till_done()

        # On arrival: logged, but the listeners are not notified yet
        assert events == ["boiler burngas ? -> on"]

        await advance(hass, freezer, 6)
    finally:
        logging.getLogger(TIMELINE).removeHandler(handler)

    assert events.index("boiler burngas ? -> on") < events.index("notify")


# Coordinator edges

async def test_coordinator_set_up_and_removed_once(hass, mqtt_mock, no_publish_delay, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    coordinator = await start(hass, {})
    assert [m for m in timeline(caplog) if m.startswith("coordinator set up (")] and len([m for m in timeline(caplog) if m.startswith("coordinator set up")]) == 1

    await coordinator.async_will_remove_from_hass()
    await coordinator.async_will_remove_from_hass()

    assert len([m for m in timeline(caplog) if m.startswith("coordinator removed (")]) == 1


# Backends without channels

async def test_submit_fallback_logs_each_supported_setter_call(hass: HomeAssistant, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    coordinator = SatFakeCoordinator(hass, {}, None, config_entry=None)
    desired = DesiredState(control_setpoint=40.0, heater_state=DeviceState.ON)

    records = await coordinator.async_submit(desired)
    first = timeline(caplog)
    caplog.clear()
    await coordinator.async_submit(desired)

    # Every call is logged: no last-sent state without channels
    assert first == ["send control_setpoint=40.0", "send heater_state=ON"]
    assert timeline(caplog) == first
    assert [(r.quantity, r.value, r.kind) for r in records] == [(Quantity.CONTROL_SETPOINT, 40.0, WriteKind.SEND), (Quantity.HEATER_STATE, DeviceState.ON, WriteKind.SEND)]


async def test_submit_fallback_does_not_log_unsupported_quantities(hass: HomeAssistant, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    coordinator = SatFakeCoordinator(hass, {}, None, config_entry=None)

    records = await coordinator.async_submit(DesiredState(maximum_relative_modulation=50, maximum_setpoint=60.0))

    assert timeline(caplog) == []
    assert records == []


# Climate loop (EMS entry)

def index_of(entries: list[str], prefix: str) -> int:
    return next(i for i, entry in enumerate(entries) if entry.startswith(prefix))


@pytest.mark.parametrize(*ems_entry())
async def test_ordering_boiler_message_then_cycle_and_send_before_publish(loop_entry, climate, boiler_data, mqtt_mock, published, caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    caplog.clear()
    events: list[str] = []

    class Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            events.append("log " + record.getMessage())

    handler = Handler()
    logging.getLogger(TIMELINE).addHandler(handler)
    real = mqtt_mock.async_publish.side_effect

    async def record_publish(topic, payload, *args, **kwargs):
        events.append(f"publish {payload}")
        if real is not None:
            return await real(topic, payload, *args, **kwargs)

    mqtt_mock.async_publish.side_effect = record_publish
    try:
        await boiler_data(burngas="on")
        await climate.async_control_heating_loop()
    finally:
        logging.getLogger(TIMELINE).removeHandler(handler)

    assert index_of(events, "log boiler burngas") < index_of(events, "log cycle ")
    assert index_of(events, "log send control_setpoint=35") < index_of(events, 'publish {"cmd": "selflowtemp", "value": 35}')
    assert index_of(events, "log send heater_state=ON") < index_of(events, 'publish {"cmd": "heatingoff", "value": "off"}')


@pytest.mark.parametrize(*ems_entry())
async def test_old_write_lines_do_not_appear(loop_entry, climate, boiler_data, mqtt_mock, published, caplog):
    caplog.set_level(logging.DEBUG)
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await run_loop(climate, mqtt_mock, published)

    assert "Wrote " not in caplog.text
    assert "Publishing MQTT command" not in caplog.text


@pytest.mark.parametrize(*ems_entry())
async def test_cold_setpoint_send_shows_requested_value(loop_entry, climate, boiler_data, mqtt_mock, published, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    await boiler_data(**IDLE)

    await run_loop(climate, mqtt_mock, published, target=15.0)

    assert "send control_setpoint=0 (requested 10)" in timeline(caplog)
    assert any(m.startswith("send heater_state=OFF") for m in timeline(caplog))


@pytest.mark.parametrize(*ems_entry())
async def test_one_debug_cycle_line_per_loop_with_consecutive_numbers(loop_entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    caplog.clear()

    numbers = []
    for _ in range(3):
        freezer.tick(1)
        await boiler_data(heatingoff="on")
        caplog.clear()
        await climate.async_control_heating_loop()
        lines = cycle_lines(caplog)
        assert len(lines) == 1
        numbers.append(int(re.match(r"cycle (\d+) \|", lines[0]).group(1)))

    assert numbers == [numbers[0], numbers[0] + 1, numbers[0] + 2]
    assert numbers[0] >= 1


@pytest.mark.parametrize(*ems_entry())
async def test_heat_cycle_line_segments(loop_entry, climate, boiler_data, mqtt_mock, published, caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    caplog.clear()

    await climate.async_control_heating_loop()

    [line] = cycle_lines(caplog)
    parts = segments(line)
    assert parts[1].startswith("in ") or parts[1].startswith("input")
    assert "target=21" in parts[1] and "inside=20.9" in parts[1] and "out=9.9" in parts[1]
    assert any(p.startswith("boiler ") and "flow=40.5" in p for p in parts)
    assert any(p.startswith("pwm ") for p in parts)
    assert any(p.startswith("setpoint calc=") and re.search(r"\(.+\)", p) for p in parts)
    want = next(p for p in parts if p.startswith("want "))
    assert "setpoint=35.0" in want and "heater=ON" in want
    sent = next(p for p in parts if p.startswith("sent "))
    assert "control_setpoint=35" in sent and "heater_state=ON" in sent


@pytest.mark.parametrize(*ems_entry())
async def test_sent_is_dash_and_resend_marked(loop_entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    await boiler_data(**IDLE | {"heatingoff": "off", "burnmaxpower": 100})
    await run_loop(climate, mqtt_mock, published)

    # heater and modulation already read back as wanted, EMS rewrites the control setpoint every cycle
    freezer.tick(1)
    await boiler_data(heatingoff="off", selflowtemp=35)
    caplog.clear()
    await climate.async_control_heating_loop()
    [line] = cycle_lines(caplog)
    sent = next(p for p in segments(line) if p.startswith("sent "))
    assert "control_setpoint=35(rewrite)" in sent
    # The rewrite is not an INFO line
    assert not [m for m in timeline(caplog) if m.startswith(("send ", "resend "))]

    # A boiler that reverts the setpoint is a drift: verify is shown and the same value is resent
    freezer.tick(1)
    await boiler_data(selflowtemp=50)
    caplog.clear()
    await climate.async_control_heating_loop()
    [line] = cycle_lines(caplog)
    assert any(p.startswith("verify ") and "control_setpoint IN_SYNC->DRIFTED" in p for p in segments(line))
    assert "resend control_setpoint=35 (read back 50.0)" in timeline(caplog)
    assert any(p.startswith("sent ") and "control_setpoint=35(resend)" in p for p in segments(line))
    assert any(m.startswith("channel control_setpoint IN_SYNC -> DRIFTED") for m in timeline(caplog))


@pytest.mark.parametrize(*ems_entry())
async def test_hvac_off_short_form(loop_entry, climate, boiler_data, mqtt_mock, published, caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    await boiler_data(**IDLE | {"heatingoff": "off"})
    await climate.async_set_hvac_mode(HVACMode.OFF)
    caplog.clear()

    await climate.async_control_heating_loop()

    [line] = cycle_lines(caplog)
    parts = segments(line)
    assert re.fullmatch(r"cycle \d+", parts[0])
    assert parts[1] == "hvac OFF"
    assert parts[2].startswith("want ") and "heater=OFF" in parts[2]
    assert parts[3].startswith("sent ")
    assert not any(p.startswith(("in ", "boiler ", "pwm ")) for p in parts)


@pytest.mark.parametrize(*ems_entry())
async def test_cycle_line_is_debug_only(loop_entry, climate, boiler_data, mqtt_mock, published, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await run_loop(climate, mqtt_mock, published)

    assert not [r for r in caplog.get_records("call") if r.name == TIMELINE and r.levelno < logging.INFO]
    assert cycle_lines(caplog) == []


@pytest.mark.parametrize(*ems_entry())
@pytest.mark.usefixtures("no_input_grace")
async def test_missing_temperature_short_form(hass, loop_entry, climate, boiler_data, mqtt_mock, published, caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    await boiler_data(**IDLE)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    hass.states.async_set("sensor.test_outside_sensor", "unavailable")
    await hass.async_block_till_done()
    caplog.clear()

    await climate.async_control_heating_loop()

    [line] = cycle_lines(caplog)
    assert re.fullmatch(r"cycle \d+ \| missing temperature: outside \| want setpoint=10 heater=OFF \| sent .*", line)


# PWM and flame edges, branches and noise

@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_pwm_cycle_line_has_state_duty_and_branch_and_edges_log_once(hass, loop_entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await run_loop(climate, mqtt_mock, published)

    pwm_part = next(p for line in cycle_lines(caplog) for p in segments(line) if p.startswith("pwm "))
    assert "%" in pwm_part
    branch = re.search(r"\(([^)]+)\)$", pwm_part)
    assert branch is not None and branch.group(1) in BRANCHES, pwm_part
    assert len([m for m in timeline(caplog) if m.startswith("pwm ON (")]) == 1
    assert "pwm OFF" not in timeline(caplog)

    freezer.tick(1)
    await boiler_data(heatingoff="off")
    await advance(hass, freezer, 1800)
    assert timeline(caplog).count("pwm OFF") == 1


@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_steady_pwm_off_phase_emits_no_per_cycle_info_lines(hass, loop_entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await run_loop(climate, mqtt_mock, published)
    freezer.tick(1)
    await boiler_data(heatingoff="off")
    await advance(hass, freezer, 1800)
    caplog.set_level(logging.INFO, logger=TIMELINE)
    # Let the boiler settle into the off phase
    for _ in range(2):
        freezer.tick(1)
        await boiler_data(heatingoff="on", selflowtemp=0)
        await climate.async_control_heating_loop()
    caplog.clear()

    for _ in range(4):
        freezer.tick(1)
        await boiler_data(heatingoff="on", selflowtemp=0)
        await climate.async_control_heating_loop()

    noisy = [m for m in timeline(caplog)]
    assert noisy == [], noisy


@pytest.mark.parametrize(*ems_entry())
async def test_control_setpoint_release_edge_logged_once(loop_entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    await boiler_data(**IDLE)
    # Only the edges after the first HEAT cycle count
    await run_loop(climate, mqtt_mock, published, target=15.0)
    caplog.clear()

    for _ in range(3):
        freezer.tick(1)
        await boiler_data(heatingoff="on", selflowtemp=0)
        await climate.async_control_heating_loop()

    assert timeline(caplog).count("control setpoint released (heater off)") == 1
    assert "release control_setpoint" in timeline(caplog)
    assert "control setpoint requested again" not in timeline(caplog)

    await climate.async_set_target_temperature(21.0)
    for _ in range(3):
        freezer.tick(1)
        await boiler_data(heatingoff="on", selflowtemp=0)
        await climate.async_control_heating_loop()

    assert timeline(caplog).count("control setpoint requested again") == 1
    assert timeline(caplog).count("control setpoint released (heater off)") == 1


# PWM.duty_cycle_branch, with the names the spec lists

async def test_duty_cycle_branch_names_match_the_spec():
    from tests.test_pwm_cycles import Clock, make_pwm, step

    clock = Clock()
    seen = {}
    for label, kwargs, duty in [
        ("low-duty flame", {"flame_active": True}, 0.0),
        ("low-duty no flame", {"flame_active": False}, 0.0),
        ("max", {}, 1.0),
    ]:
        pwm = make_pwm()
        assert pwm.duty_cycle_branch is None
        await step(pwm, clock, duty, **kwargs)
        seen[label] = pwm.duty_cycle_branch

    assert seen == {"low-duty flame": "low-duty", "low-duty no flame": "low-duty", "max": "max"}


async def test_manual_duty_cycle_branch():
    from tests.test_pwm_cycles import Clock, make_pwm, step

    pwm = make_pwm(automatic=False)
    await step(pwm, Clock(), 0.5)

    assert pwm.duty_cycle_branch == "manual"


# Round 2: coordinator removal, overshoot, release edges, logging never raises

async def test_removed_is_not_logged_when_the_start_failed(hass, caplog):
    # Without the MQTT mock the start fails
    caplog.set_level(logging.INFO, logger=TIMELINE)
    coordinator = await start(hass, {})
    assert not [m for m in timeline(caplog, phase="call") if m.startswith("coordinator set up")]
    assert any(r.levelno == logging.ERROR and "Failed to start" in r.getMessage() for r in caplog.get_records("call"))

    await coordinator.async_will_remove_from_hass()

    assert not [m for m in timeline(caplog, phase="call") if m.startswith("coordinator removed")]


@pytest.mark.parametrize(*ems_entry())
async def test_overshoot_handling_edges_are_logged_once_each(loop_entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.INFO, logger=TIMELINE)
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    caplog.clear()

    status = PropertyMock(return_value=BoilerStatus.OVERSHOOT_HANDLING)
    with patch.object(SatEmsMqttCoordinator, "device_status", status):
        for _ in range(3):
            freezer.tick(1)
            await boiler_data(heatingoff="on")
            await climate.async_control_heating_loop()

        assert timeline(caplog).count("overshoot handling started") == 1
        assert "overshoot handling ended" not in timeline(caplog)

        status.return_value = BoilerStatus.AT_SETPOINT
        for _ in range(3):
            freezer.tick(1)
            await boiler_data(heatingoff="on")
            await climate.async_control_heating_loop()

    assert timeline(caplog).count("overshoot handling started") == 1
    assert timeline(caplog).count("overshoot handling ended") == 1
    assert "Overshoot Handling detected" not in caplog.text


def release_lines(caplog) -> list[str]:
    return [m for m in timeline(caplog) if m.startswith("control setpoint")]


@pytest.mark.parametrize(*ems_entry())
async def test_release_edges_follow_hvac_mode_changes_with_reasons(loop_entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    await boiler_data(**IDLE | {"heatingoff": "off", "burnmaxpower": 100})

    # The entry starts in HVAC OFF: nothing was requested yet, so nothing is logged for the release, nor for the first request
    assert not [r for r in caplog.records if r.getMessage().startswith("control setpoint")]
    await run_loop(climate, mqtt_mock, published, target=21.0)
    assert release_lines(caplog) == []
    assert "send control_setpoint=35" in timeline(caplog)

    # HVAC OFF releases once, however many cycles it lasts
    await climate.async_set_hvac_mode(HVACMode.OFF)
    for _ in range(3):
        freezer.tick(1)
        await boiler_data(heatingoff="on")
        await climate.async_control_heating_loop()
    assert release_lines(caplog) == ["control setpoint released (hvac off)"]

    # Back to HEAT with the heater ON requests the setpoint again, once
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    for _ in range(3):
        freezer.tick(1)
        await boiler_data(heatingoff="off")
        await climate.async_control_heating_loop()
    assert release_lines(caplog) == ["control setpoint released (hvac off)", "control setpoint requested again"]


@pytest.mark.parametrize(*ems_entry())
async def test_heater_off_in_heat_releases_with_that_reason_once(loop_entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    await boiler_data(**IDLE)
    await run_loop(climate, mqtt_mock, published, target=15.0)

    for _ in range(4):
        freezer.tick(1)
        await boiler_data(heatingoff="on", selflowtemp=0)
        await climate.async_control_heating_loop()

    assert release_lines(caplog) == ["control setpoint released (heater off)"]


@pytest.mark.parametrize(*otgw_entry())
async def test_backend_without_channels_never_logs_release_edges(hass, loop_entry, climate, mqtt_mock, published, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    async_fire_mqtt_message(hass, "otgw/value/gw1/centralheating", "ON")
    await hass.async_block_till_done()

    await run_loop(climate, mqtt_mock, published, target=15.0)
    await run_loop(climate, mqtt_mock, published, target=21.0)
    await run_loop(climate, mqtt_mock, published, target=21.0, mode=HVACMode.OFF)
    await run_loop(climate, mqtt_mock, published, target=15.0)

    assert release_lines(caplog) == []
    assert any(m.startswith("send ") for m in timeline(caplog))


@pytest.mark.parametrize(*ems_entry())
async def test_failure_building_the_cycle_line_does_not_stop_the_loop(loop_entry, climate, boiler_data, mqtt_mock, published, freezer, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    monkeypatch.setattr(timeline_module, "_failure_logged", False)

    def boom(_state):
        raise RuntimeError("cannot format")

    monkeypatch.setattr(timeline_module, "desired", boom)
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    caplog.clear()
    mqtt_mock.async_publish.reset_mock()

    with patch.object(climate, "async_write_ha_state", wraps=climate.async_write_ha_state) as write_state:
        await climate.async_control_heating_loop()

        # Control behaviour is unchanged: the writes happened and the entity state was written
        assert [c[1] for c in published()] == ['{"cmd": "selflowtemp", "value": 35}', '{"cmd": "burnmaxpower", "value": 100}', '{"cmd": "heatingoff", "value": "off"}']
        assert write_state.call_count == 1

        failures = [r for r in caplog.get_records("call") if r.name == TIMELINE and r.levelno >= logging.WARNING]
        assert [(r.levelno, r.exc_info is not None) for r in failures] == [(logging.ERROR, True)]

        # Later failures are one-line warnings
        caplog.clear()
        freezer.tick(1)
        await boiler_data(heatingoff="on")
        await climate.async_control_heating_loop()
        failures = [r for r in caplog.get_records("call") if r.name == TIMELINE and r.levelno >= logging.WARNING]
        assert [(r.levelno, r.exc_info is not None) for r in failures] == [(logging.WARNING, False)]
        assert write_state.call_count == 2


@pytest.mark.parametrize(*ems_entry())
async def test_cycle_line_is_only_built_when_debug_is_enabled(loop_entry, climate, boiler_data, mqtt_mock, published, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    built = []
    real = timeline_module.desired

    def counting(state):
        built.append(state)
        return real(state)

    monkeypatch.setattr(timeline_module, "desired", counting)
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await run_loop(climate, mqtt_mock, published)
    assert built == []

    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    await climate.async_control_heating_loop()
    assert len(built) == 1


class Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("no str")


async def test_unprintable_value_in_a_boiler_change_does_not_raise(hass, mqtt_mock, no_publish_delay, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    monkeypatch.setattr(timeline_module, "_failure_logged", False)
    coordinator = await start(hass, {})
    caplog.clear()

    coordinator.async_set_updated_data({"heatingactive": Unprintable()})
    coordinator.async_set_updated_data({"burngas": Unprintable()})

    # The data is stored; the first failure has a traceback, the later one doesn't
    assert isinstance(coordinator.data["heatingactive"], Unprintable)
    failures = [r for r in caplog.records if r.name == TIMELINE and r.levelno >= logging.WARNING]
    assert [(r.levelno, r.exc_info is not None) for r in failures] == [(logging.ERROR, True), (logging.WARNING, False)]


def test_unknown_desired_field_is_shown_by_its_own_name():
    @dataclass(frozen=True)
    class Extended:
        control_setpoint: float | None = None
        brand_new_field: int | None = None

    assert timeline_module.desired(Extended(control_setpoint=40.0, brand_new_field=3)) == "setpoint=40.0 brand_new_field=3"  # type: ignore[arg-type]
    assert timeline_module.desired(Extended()) == "-"  # type: ignore[arg-type]


# Round 3: no per-cycle repeats in steady states

STEADY_CYCLES = 8


def per_cycle_messages(caplog, cycle_records: list[list[logging.LogRecord]]) -> list[str]:
    """Messages (digits stripped) that occur in each of the last 6 cycles, apart from the cycle line."""
    sets = []
    for records in cycle_records[-6:]:
        sets.append({re.sub(r"[\d.]+", "#", f"{r.name} {r.levelname} {r.getMessage()}") for r in records if r.name.startswith("custom_components.sat") and not r.getMessage().startswith("cycle ")})
    return sorted(set.intersection(*sets)) if sets else []


async def run_steady(climate, boiler_data, freezer, caplog, cycles: int = STEADY_CYCLES, **data) -> list[list[logging.LogRecord]]:
    caplog.set_level(logging.DEBUG, logger="custom_components.sat")
    records = []
    for _ in range(cycles):
        freezer.tick(1)
        await boiler_data(**data)
        caplog.clear()
        await climate.async_control_heating_loop()
        records.append(list(caplog.get_records("call")))
    return records


@pytest.mark.parametrize(*ems_entry())
async def test_steady_heater_on_repeats_nothing_every_cycle(loop_entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
    await boiler_data(**IDLE | {"heatingoff": "off", "burnmaxpower": 100})
    await run_loop(climate, mqtt_mock, published, target=21.0)

    records = await run_steady(climate, boiler_data, freezer, caplog, heatingoff="off", selflowtemp=35)

    assert per_cycle_messages(caplog, records) == []


@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_steady_pwm_off_phase_repeats_nothing_every_cycle(hass, loop_entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await run_loop(climate, mqtt_mock, published)
    freezer.tick(1)
    await boiler_data(heatingoff="off")
    await advance(hass, freezer, 1800)
    assert climate.pwm.status.name == "OFF"
    for _ in range(2):
        freezer.tick(1)
        await boiler_data(heatingoff="on", selflowtemp=0)
        await climate.async_control_heating_loop()

    records = await run_steady(climate, boiler_data, freezer, caplog, heatingoff="on", selflowtemp=0)

    assert per_cycle_messages(caplog, records) == []


@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_steady_pwm_on_phase_with_flame_repeats_nothing_every_cycle(hass, loop_entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
    on = {"heatingoff": "off", "heatingactive": "on", "burngas": "on", "curflowtemp": 45.5, "selflowtemp": 30}
    await boiler_data(**IDLE | on)
    await run_loop(climate, mqtt_mock, published)
    assert climate.pwm.status.name == "ON"

    records = await run_steady(climate, boiler_data, freezer, caplog, cycles=7, **on)

    assert climate.pwm.status.name == "ON"
    assert per_cycle_messages(caplog, records) == []


@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_minimum_setpoint_is_logged_only_when_it_changes(hass, loop_entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
    on = {"heatingoff": "off", "heatingactive": "on", "burngas": "on", "curflowtemp": 45.5, "selflowtemp": 30}
    await boiler_data(**IDLE | on)
    await run_loop(climate, mqtt_mock, published)

    # With the boiler heating up, the dynamic minimum setpoint is calculated every cycle
    with patch.object(SatEmsMqttCoordinator, "device_status", PropertyMock(return_value=BoilerStatus.HEATING_UP)):
        records = await run_steady(climate, boiler_data, freezer, caplog, cycles=10, **on)

    calculated = [r for rs in records for r in rs if r.getMessage().startswith("Calculated new minimum setpoint")]
    assert 1 <= len(calculated) < 10
    assert per_cycle_messages(caplog, records) == []
    assert not [r for rs in records for r in rs if "Updated last boiler temperature" in r.getMessage()]
