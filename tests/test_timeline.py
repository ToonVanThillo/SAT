"""The timeline logger: the info lines that remain (boiler changes, flame, coordinator) and the structured `cycle` record of each control-loop
run, with its rendering, from the spec (docs/design.md, Timeline)."""
import logging
import re
from typing import Any
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.sat.channels import (
    RELEASE,
    ChannelStatus,
    DesiredState,
    Quantity,
    WriteKind,
)
from custom_components.sat.const import (
    CONF_ALLOW_UNRESTRICTED_MODULATION,
    CONF_MINIMUM_SETPOINT,
    BoilerStatus,
    PWMStatus,
)
from custom_components.sat.coordinator import DeviceState
from custom_components.sat.fake import SatFakeCoordinator
from custom_components.sat.mqtt.ems import SatEmsMqttCoordinator
from tests.conftest import TIMELINE, advance, cycle_records, last_cycle
from tests.const import IDLE, ems_entry, with_data
from tests.test_climate_loop import message, run_loop
from tests.test_mqtt_ems import start

HEAT_BOILER = IDLE | {"heatingoff": "on"}


def timeline(caplog, level=logging.INFO, phase="call") -> list[str]:
    return [r.getMessage() for r in caplog.get_records(phase) if r.name == TIMELINE and r.levelno == level]


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

async def test_submit_without_channels_returns_its_records_and_logs_nothing(hass: HomeAssistant, caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    coordinator = SatFakeCoordinator(hass, {}, None, config_entry=None)
    desired = DesiredState(control_setpoint=40.0, heater_state=DeviceState.ON)

    records = await coordinator.async_submit(desired)
    await coordinator.async_submit(desired)

    assert [(r.quantity, r.value, r.kind) for r in records] == [(Quantity.CONTROL_SETPOINT, 40.0, WriteKind.SEND), (Quantity.HEATER_STATE, DeviceState.ON, WriteKind.SEND)]
    assert [r for r in caplog.records if r.name == TIMELINE] == []


async def test_submit_fallback_ignores_unsupported_quantities(hass: HomeAssistant, caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    coordinator = SatFakeCoordinator(hass, {}, None, config_entry=None)

    records = await coordinator.async_submit(DesiredState(maximum_relative_modulation=50, maximum_setpoint=60.0))

    assert [r for r in caplog.records if r.name == TIMELINE] == []
    assert records == []


async def test_removed_is_not_logged_when_the_start_failed(hass, caplog):
    # Without the MQTT mock the start fails
    caplog.set_level(logging.INFO, logger=TIMELINE)
    coordinator = await start(hass, {})
    assert not [m for m in timeline(caplog, phase="call") if m.startswith("coordinator set up")]
    assert any(r.levelno == logging.ERROR and "Failed to start" in r.getMessage() for r in caplog.get_records("call"))

    await coordinator.async_will_remove_from_hass()

    assert not [m for m in timeline(caplog, phase="call") if m.startswith("coordinator removed")]


# The cycle record: one per run

@pytest.fixture
def heating(hass, loop_entry, climate, boiler_data, caplog):
    """The EMS climate in HEAT at target 21 with the timeline at debug and the debounced loops of the setup done."""
    caplog.set_level(logging.DEBUG, logger=TIMELINE)

    async def go(**data: Any) -> None:
        await boiler_data(**HEAT_BOILER | data)
        await climate.async_set_target_temperature(21.0)
        await climate.async_set_hvac_mode(HVACMode.HEAT)
        await hass.async_block_till_done()
        caplog.clear()

    return go


@pytest.mark.parametrize(*ems_entry())
async def test_one_debug_record_per_run_with_consecutive_numbers(heating, climate, caplog):
    await heating()

    numbers = []
    for _ in range(3):
        before = len(caplog.records)
        await climate.async_control_heating_loop()
        [record] = cycle_records(caplog, before)
        assert record.levelno == logging.DEBUG
        assert record.name == TIMELINE
        assert next(iter(record.fields)) == "event"  # type: ignore[attr-defined]
        assert "error" not in record.fields  # type: ignore[attr-defined]
        assert record.getMessage().startswith("cycle ")
        numbers.append(record.fields["cycle"])  # type: ignore[attr-defined]

    assert numbers == [numbers[0], numbers[0] + 1, numbers[0] + 2]


@pytest.mark.parametrize(*ems_entry())
async def test_a_failing_run_logs_a_record_with_the_error_and_the_fields_so_far_and_raises(heating, climate, coordinator, caplog):
    await heating()

    with patch.object(coordinator, "async_submit", side_effect=RuntimeError('boom "now"')), pytest.raises(RuntimeError, match="boom"):
        await climate.async_control_heating_loop()

    [record] = cycle_records(caplog)
    fields = record.fields  # type: ignore[attr-defined]
    assert fields["error"] == 'RuntimeError: boom "now"'
    assert fields["hvac"] == HVACMode.HEAT
    assert fields["inputs"].current_temperature == 20.9
    assert fields["want"].heater_state == DeviceState.ON
    assert fields["pwm"].status == PWMStatus.ON
    assert "sent" not in fields
    assert r'error="RuntimeError: boom \"now\""' in record.getMessage()


@pytest.mark.parametrize(*ems_entry())
async def test_the_debounced_run_executes_the_loop_body(heating, hass, climate, caplog, freezer, mqtt_mock, published):
    await heating()
    mqtt_mock.async_publish.reset_mock()

    climate.schedule_control_heating_loop()
    await hass.async_block_till_done()
    assert cycle_records(caplog) == []

    await advance(hass, freezer, 11)

    [record] = cycle_records(caplog)
    assert record.fields["hvac"] == HVACMode.HEAT  # type: ignore[attr-defined]
    assert message('{"cmd": "selflowtemp", "value": 35}') in published()


# The fields per branch

COMMON = ["cycle", "hvac", "inputs", "boiler", "verify", "pwm", "want", "sent"]
HEAT_ONLY = ["smoothed", "calculated", "curve", "p", "i", "d"]


@pytest.mark.parametrize(*ems_entry())
async def test_hvac_off_has_the_common_fields_only(heating, climate, caplog):
    await heating()
    await climate.async_set_hvac_mode(HVACMode.OFF)
    caplog.clear()

    await climate.async_control_heating_loop()

    fields = last_cycle(caplog)
    assert fields["hvac"] == HVACMode.OFF
    assert fields["want"] == DesiredState(control_setpoint=RELEASE, heater_state=DeviceState.OFF)
    assert [key for key in fields if key != "event"] == COMMON


@pytest.mark.parametrize(*ems_entry())
@pytest.mark.usefixtures("no_input_grace")
async def test_a_missing_temperature_shows_in_the_inputs_and_has_no_setpoint_fields(heating, hass, climate, caplog):
    await heating()
    hass.states.async_set("sensor.test_inside_sensor", "unavailable")
    await hass.async_block_till_done()
    caplog.clear()

    await climate.async_control_heating_loop()

    fields = last_cycle(caplog)
    assert fields["inputs"].current_temperature is None
    assert fields["pwm"].status == PWMStatus.OFF
    assert fields["want"] == DesiredState(control_setpoint=RELEASE, heater_state=DeviceState.OFF)
    assert [key for key in fields if key != "event"] == COMMON


@pytest.mark.parametrize(*ems_entry())
async def test_heat_adds_the_setpoint_fields(heating, climate, caplog):
    await heating()

    await climate.async_control_heating_loop()

    fields = last_cycle(caplog)
    assert {key for key in fields if key != "event"} == set(COMMON + HEAT_ONLY)
    inputs = fields["inputs"]
    assert (inputs.target_temperature, inputs.current_temperature, inputs.outside_temperature, inputs.valves_open) == (21.0, 20.9, 9.9, True)
    assert inputs.held == ()
    assert isinstance(fields["boiler"].status, BoilerStatus)
    assert (fields["boiler"].flame_active, fields["boiler"].hot_water_active) == (False, False)
    assert fields["pwm"].status == PWMStatus.ON
    assert (fields["pwm"].duty_cycle, fields["pwm"].duty_cycle_branch, fields["pwm"].last_duty_cycle_percentage) == ((3600, 0), "unrestricted", 100.0)
    assert fields["smoothed"] == fields["calculated"] == 35.0
    assert fields["want"].control_setpoint == 35.0


@pytest.mark.parametrize(*ems_entry())
async def test_the_boiler_state_has_an_unknown_flow_as_none(heating, climate, caplog, boiler_data):
    await heating()
    await boiler_data(curflowtemp=None)

    await climate.async_control_heating_loop()

    assert last_cycle(caplog)["boiler"].flow_temperature is None


@pytest.mark.parametrize(*ems_entry())
async def test_a_released_setpoint_shows_in_the_want_and_is_not_sent(heating, climate, caplog, boiler_data):
    await boiler_data(**IDLE)
    await climate.async_set_target_temperature(15.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    caplog.clear()

    await climate.async_control_heating_loop()

    fields = last_cycle(caplog)
    assert fields["want"].control_setpoint == RELEASE
    sent = {record.quantity: record for record in fields["sent"]}
    assert Quantity.CONTROL_SETPOINT not in sent
    assert sent[Quantity.HEATER_STATE].value == DeviceState.OFF


@pytest.mark.parametrize(*ems_entry())
async def test_sent_marks_rewrites_and_resends_with_the_read_back(heating, climate, caplog, boiler_data, freezer):
    await heating()
    await climate.async_control_heating_loop()
    first = {record.quantity: record for record in last_cycle(caplog)["sent"]}
    assert (first[Quantity.CONTROL_SETPOINT].value, first[Quantity.CONTROL_SETPOINT].kind) == (35, WriteKind.SEND)

    freezer.tick(5)
    await boiler_data(**HEAT_BOILER | {"selflowtemp": 35, "burnmaxpower": 100, "heatingoff": "off"})
    await climate.async_control_heating_loop()
    again = {record.quantity: record for record in last_cycle(caplog)["sent"]}
    assert (again[Quantity.CONTROL_SETPOINT].value, again[Quantity.CONTROL_SETPOINT].kind) == (35, WriteKind.REWRITE)

    # The boiler answers the heater with the opposite of what was sent: the next cycle sends it again, with the read-back
    await climate.async_set_target_temperature(15.0)
    await climate.async_control_heating_loop()
    freezer.tick(5)
    await boiler_data(**HEAT_BOILER | {"heatingoff": "off", "selflowtemp": 0})
    await climate.async_control_heating_loop()
    heater = {record.quantity: record for record in last_cycle(caplog)["sent"]}[Quantity.HEATER_STATE]
    assert (heater.value, heater.kind, heater.read_back) == (DeviceState.OFF, WriteKind.RESEND, DeviceState.ON)


@pytest.mark.parametrize(*ems_entry())
async def test_verify_lists_the_status_changes_with_what_was_sent_and_read_back(heating, climate, caplog, boiler_data, freezer):
    await boiler_data(**HEAT_BOILER)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()

    freezer.tick(5)
    await boiler_data(**HEAT_BOILER | {"selflowtemp": 35, "burnmaxpower": 100, "heatingoff": "off"})
    await climate.async_control_heating_loop()
    [change] = [c for c in last_cycle(caplog)["verify"] if c.quantity == Quantity.CONTROL_SETPOINT]
    assert (change.previous, change.current, change.sent, change.read_back) == (ChannelStatus.PENDING, ChannelStatus.IN_SYNC, 35, 35)

    freezer.tick(5)
    await boiler_data(**HEAT_BOILER | {"selflowtemp": 35, "burnmaxpower": 100, "heatingoff": "off"})
    await climate.async_control_heating_loop()
    assert last_cycle(caplog)["verify"] == []


@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}))
async def test_the_pwm_state_shows_a_pulse_held_on_for_the_minimum_pulse(heating, climate, caplog, freezer):
    await heating(curflowtemp=80.0)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.ON
    assert last_cycle(caplog)["pwm"].minimum_pulse_held is False

    # A lower target gives a smoothed setpoint of 29.1, just above the cold threshold: against the hot flow a duty with no on time; the
    # pulse is within its minimum
    freezer.tick(10)
    await climate.async_set_target_temperature(20.75)
    await climate.async_control_heating_loop()

    assert last_cycle(caplog)["pwm"].status == PWMStatus.ON
    assert last_cycle(caplog)["pwm"].minimum_pulse_held is True


@pytest.mark.parametrize(*ems_entry())
async def test_the_pwm_state_shows_a_rollover_only_in_its_cycle(heating, climate, caplog, freezer, boiler_data):
    await heating()
    await climate.async_control_heating_loop()
    assert last_cycle(caplog)["pwm"].rolled_over is False

    freezer.tick(3600)
    await boiler_data(**HEAT_BOILER)
    await climate.async_control_heating_loop()
    assert last_cycle(caplog)["pwm"].rolled_over is True
    assert climate.pwm.status == PWMStatus.ON

    freezer.tick(10)
    await climate.async_control_heating_loop()
    assert last_cycle(caplog)["pwm"].rolled_over is False


@pytest.mark.parametrize(*ems_entry())
async def test_the_record_keeps_the_objects_and_the_message_renders_them(heating, climate, caplog, boiler_data):
    await heating()
    await boiler_data(curflowtemp=40.26)

    await climate.async_control_heating_loop()

    [record] = cycle_records(caplog)
    fields = record.fields  # type: ignore[attr-defined]
    text = record.getMessage()
    assert fields["boiler"].flow_temperature == 40.26
    assert 'hvac="HEAT"' in text
    assert "flow_temperature:40.3" in text
    assert "flame_active:false" in text
    assert "valves_open:true" in text
    assert 'held:[]' in text
    assert f"status:{fields['boiler'].status.name}" in text
    assert re.search(r'want="\{control_setpoint:35\.0, heater_state:ON, [^"]*\}"', text)
    # Keys come in the order the fields were added
    assert [m.group(1) for m in re.finditer(r'(\w+)="', text)][:4] == ["cycle", "hvac", "inputs", "boiler"]


@pytest.mark.parametrize(*ems_entry())
async def test_the_cycle_record_is_logged_when_the_run_ends_after_the_boiler_line_and_the_publishes(heating, hass, climate, mqtt_mock, boiler_data):
    await heating(burngas="off")
    order: list[str] = []
    real = mqtt_mock.async_publish.side_effect

    async def record_publish(topic, payload, *args, **kwargs):
        order.append(f"publish {payload}")
        if real is not None:
            return await real(topic, payload, *args, **kwargs)

    class Order(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            order.append("log " + record.getMessage().split(" ")[0] + " " + record.getMessage().split(" ")[1])

    handler = Order()
    logging.getLogger(TIMELINE).addHandler(handler)
    mqtt_mock.async_publish.side_effect = record_publish
    try:
        await boiler_data(burngas="on")
        await climate.async_control_heating_loop()
    finally:
        logging.getLogger(TIMELINE).removeHandler(handler)

    cycle_at = next(i for i, entry in enumerate(order) if entry.startswith("log cycle"))
    assert order.index("log boiler burngas") < cycle_at
    assert all(i < cycle_at for i, entry in enumerate(order) if entry.startswith("publish"))


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


# No per-cycle repeats in steady states

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


@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}))
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


@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}))
async def test_steady_pwm_on_phase_with_flame_repeats_nothing_every_cycle(hass, loop_entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
    on = {"heatingoff": "off", "heatingactive": "on", "burngas": "on", "curflowtemp": 45.5, "selflowtemp": 30}
    await boiler_data(**IDLE | on)
    await run_loop(climate, mqtt_mock, published)
    assert climate.pwm.status.name == "ON"

    records = await run_steady(climate, boiler_data, freezer, caplog, cycles=7, **on)

    assert climate.pwm.status.name == "ON"
    assert per_cycle_messages(caplog, records) == []


@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}))
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
