"""Circulation after hot water, second part: the start window, the minimum relative modulation, and what the cycles around a circulation
request and publish, with the read-backs of a real boiler.

The room is kept so that the normal decision is heater OFF without PWM (calculated setpoint "too cold"): inside 15, target 15.5, outside 25
on an underfloor system. The normal relative modulation is then the configured maximum, which circulation must replace by the minimum.
"""
import json
import logging
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import HVACMode

from custom_components.sat.const import (
    CONF_CIRCULATE_AFTER_HOT_WATER,
    CONF_CIRCULATION_DURATION,
    CONF_HEATING_SYSTEM,
    CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR,
    CONF_RADIATORS,
    HEATING_SYSTEM_UNDERFLOOR,
    MINIMUM_RELATIVE_MODULATION,
    PWMStatus,
)
from custom_components.sat.flame import Flame
from tests.conftest import TIMELINE, run_one_loop, timeline_since
from tests.const import IDLE, with_data
from tests.test_circulation import (
    DRAW,
    END,
    PUMP,
    START,
    Env,
    lines,
    warnings,
)

INSIDE = "sensor.test_inside_sensor"
OUTSIDE = "sensor.test_outside_sensor"
RADIATOR = "climate.radiator"
RELEASED = "control setpoint released (heater off)"
REQUESTED_AGAIN = "control setpoint requested again"
SETPOINT_20 = '{"cmd": "selflowtemp", "value": 20}'

# The window of a start: the time since the hot water burn ended plus the duration stays below this
WINDOW = Flame.STUCK_OFF_SECONDS


@pytest.fixture(autouse=True)
def timeline_debug(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)


@pytest.fixture
def env(freezer: FrozenDateTimeFactory, caplog) -> Env:
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    return Env(freezer)


# B. The start window (unit)

def test_the_window_is_the_flame_health_limit():
    assert WINDOW == 300


@pytest.mark.parametrize(("since", "starts"), [(0, True), (10, True), (239, True), (239.9, True), (240, False), (240.1, False), (299, False), (300, False), (5000, False), (None, False)])
def test_a_start_needs_the_time_since_the_hot_water_burn_plus_the_duration_below_the_window(env, caplog, since, starts):
    assert env.update(since=since) is starts
    assert bool(lines(caplog, START)) is starts


@pytest.mark.parametrize(("duration", "since", "starts"), [(10, 289, True), (10, 290, False), (240, 59, True), (240, 60, False), (120, 179, True), (120, 180, False)])
def test_the_window_follows_the_duration(freezer, caplog, duration, since, starts):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    env = Env(freezer, duration=duration)

    assert env.update(since=since) is starts


def test_the_window_is_a_start_condition_only(env, caplog):
    assert env.update(since=100) is True

    # The window has long passed (the cycles are far apart); the run goes on until its own end
    env.freezer.tick(30)
    assert env.update(since=400) is True
    env.freezer.tick(29)
    assert env.update(since=1000) is True
    assert lines(caplog, END) == []

    env.freezer.tick(1)
    assert env.update(since=1000) is False
    assert lines(caplog, END) == ["circulation end (duration)"]


def test_the_window_does_not_end_a_run_that_never_had_one(env, caplog):
    assert env.update(since=100) is True
    assert env.update(since=None) is True
    assert lines(caplog, END) == []


def test_a_late_start_is_blocked_while_the_wait_stays_armed(env, caplog):
    # Awaiting the pump restart long after the draw, without heat demand; then the demand appears
    assert env.update(since=1000, error=-1.0) is False
    assert env.update(since=1100, error=0.5) is False
    assert not env.circulation.active
    assert lines(caplog, START) == []


def test_a_blocked_late_start_does_not_use_up_the_burn(env, caplog):
    assert env.update(since=1000) is False

    # A new hot water burn (its time since is small again) can start
    assert env.update(burns=2, since=5) is True
    assert len(lines(caplog, START)) == 1


# B. The configuration

def test_the_duration_maximum_is_below_the_window():
    from tests.test_circulation import DURATION_MAXIMUM

    assert DURATION_MAXIMUM < WINDOW


# Climate level

def cold_room(extra: dict[str, Any] | None = None, **data: Any):
    return with_data({CONF_HEATING_SYSTEM: HEATING_SYSTEM_UNDERFLOOR} | data, {CONF_CIRCULATE_AFTER_HOT_WATER: True} | (extra or {}))


class Driver:
    """Runs cycles against the EMS boiler and reads back what was published, as a boiler does."""

    def __init__(self, hass, climate, boiler_data, freezer, mqtt_mock, published, caplog) -> None:
        self.hass = hass
        self.climate = climate
        self.boiler_data = boiler_data
        self.freezer = freezer
        self.mqtt_mock = mqtt_mock
        self.published = published
        self.caplog = caplog
        self.readback: dict[str, Any] = {"heatingoff": "on", "selflowtemp": 5}
        self.sent: list[tuple[str, Any]] = []
        self.mark = 0

    async def start(self) -> None:
        self.hass.states.async_set(INSIDE, "15")
        self.hass.states.async_set(OUTSIDE, "25")
        await self.boiler_data(**PUMP | self.readback)
        await self.climate.async_set_hvac_mode(HVACMode.HEAT)

        # Heating wanted first (the control setpoint requested and read back), then the room is warm enough: heater OFF and released
        await self.climate.async_set_target_temperature(21.0)
        await self.cycle(**PUMP)
        await self.cycle(**PUMP)
        await self.climate.async_set_target_temperature(15.5)
        for _ in range(20):
            await self.cycle(**PUMP)
            if "heater=OFF" in self.line:
                break
        await self.cycle(**PUMP)
        await self.cycle(**PUMP)
        assert "want setpoint=RELEASE heater=OFF" in self.line

    async def cycle(self, seconds: float = 10, **data: Any) -> "Driver":
        """Feed boiler data (what was read back wins), run one loop; `sent` is what it published, `new` the timeline lines it added."""
        self.mark = len(self.caplog.records)
        self.mqtt_mock.async_publish.reset_mock()
        self.freezer.tick(seconds)
        await self.boiler_data(**IDLE | data | self.readback)
        await self.hass.async_block_till_done()

        await run_one_loop(self.hass, self.climate, self.caplog, self.mark)
        self.sent = [(json.loads(p)["cmd"], json.loads(p)["value"]) for _t, p, *_ in self.published()]
        for command, value in self.sent:
            self.readback[command] = value

        return self

    @property
    def new(self) -> list[str]:
        return timeline_since(self.caplog, self.mark)

    @property
    def line(self) -> str:
        return next(m for m in reversed(self.new) if m.startswith("cycle "))

    def published_values(self, command: str) -> list[Any]:
        """The values published for `command` in the last cycle, in order."""
        return [value for name, value in self.sent if name == command]


@pytest.fixture
def driver(hass, climate, boiler_data, freezer, mqtt_mock, published, caplog) -> Driver:
    return Driver(hass, climate, boiler_data, freezer, mqtt_mock, published, caplog)


PUMP_OFF = {"heatingpump": "off", "curflowtemp": 55.0}
PUMP_ON = {"heatingpump": "on", "curflowtemp": 55.0}
FLAME_OFF = DRAW | {"burngas": "off", "tapwateractive": "off"}


async def circulate(driver: Driver) -> Driver:
    """Heating wanted but not needed (heater OFF), a hot water draw, the flame off and the pump off: the start cycle."""
    await driver.start()
    await driver.cycle(**DRAW)
    await driver.cycle(5, **FLAME_OFF | PUMP_ON)
    # No circulation yet while the pump runs
    assert not lines(driver.caplog, START)
    await driver.cycle(5, **PUMP_ON)
    await driver.cycle(5, **PUMP_OFF)

    return driver


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*cold_room())
async def test_without_circulation_the_cold_room_decides_heater_off_at_the_configured_modulation(entry, driver):
    await driver.start()

    assert "heater=OFF relmod=100" in driver.line
    assert "(too cold)" in driver.line


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*cold_room())
async def test_the_start_cycle_requests_again_and_publishes_heater_on_at_the_circulation_setpoint(entry, driver, caplog):
    await circulate(driver)

    assert len(lines(caplog, START)) == 1
    assert REQUESTED_AGAIN in driver.new
    assert "heatingoff" in [name for name, _ in driver.sent]
    assert driver.published_values("heatingoff") == ["off"]
    assert set(driver.published_values("selflowtemp")) == {20}
    assert "(circulation)" in driver.line
    assert "want setpoint=20.0 heater=ON" in driver.line
    assert RELEASED not in driver.new


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*cold_room())
async def test_the_start_cycle_requests_the_minimum_modulation_not_the_configured_maximum(entry, driver):
    await circulate(driver)

    assert f"relmod={MINIMUM_RELATIVE_MODULATION}" in driver.line
    assert 100 not in driver.published_values("burnmaxpower")
    assert driver.published_values("burnmaxpower") == [0]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*cold_room({CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR: 31}))
async def test_the_minimum_modulation_is_published_as_the_configured_floor(entry, driver):
    await circulate(driver)

    assert f"relmod={MINIMUM_RELATIVE_MODULATION}" in driver.line
    assert driver.published_values("burnmaxpower") == [31]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*cold_room({CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR: 31}))
async def test_every_circulation_cycle_requests_the_minimum_modulation_through_the_readbacks(entry, driver, caplog):
    await circulate(driver)
    cycles = [driver.line]
    assert driver.readback["selflowtemp"] == 20

    # The pump comes on, the boiler reads back 20 and the floor
    for data in (PUMP_ON, PUMP_ON, PUMP_ON):
        await driver.cycle(10, **data)
        assert "(circulation)" in driver.line
        cycles.append(driver.line)
        assert 100 not in driver.published_values("burnmaxpower")

    assert all(f"relmod={MINIMUM_RELATIVE_MODULATION}" in line for line in cycles)
    assert lines(caplog, END) == []

    # After the duration the normal decision applies unchanged: heater OFF and the configured maximum modulation again
    await driver.cycle(40, **PUMP_ON)
    assert lines(caplog, END) == ["circulation end (duration)"]
    assert "(circulation)" not in driver.line
    assert "relmod=100" in driver.line
    assert driver.published_values("burnmaxpower") == [100]

    # The boiler reads selflowtemp back to 5 afterwards: nothing is requested again
    driver.readback["selflowtemp"] = 5
    await driver.cycle(10, **PUMP_ON)
    assert START not in " ".join(driver.new)
    assert "(circulation)" not in driver.line


# The ending cycle, per end reason

def closing(hass):
    hass.states.async_set(RADIATOR, "heat", {"hvac_action": "idle", "hvac_modes": ["heat", "off"]})


ENDS = [
    ("duration", {"seconds": 60}, "duration"),
    ("flow temperature", {"data": {"curflowtemp": 20.0}}, "flow temperature"),
    ("no heat demand", {"inside": "17"}, "no heat demand"),
    ("hot water", {"data": DRAW | {"heatingpump": "on", "curflowtemp": 55.0}}, "hot water"),
    ("flame on", {"data": PUMP_ON | {"burngas": "on", "heatingactive": "on"}}, "flame on"),
]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*cold_room())
@pytest.mark.parametrize(("name", "change", "reason"), ENDS, ids=[e[0] for e in ENDS])
async def test_the_ending_cycle_applies_the_normal_decision_and_releases_once_confirmed(hass, entry, driver, caplog, name, change, reason):
    await circulate(driver)
    await driver.cycle(10, **PUMP_ON)
    assert lines(caplog, END) == []

    if "inside" in change:
        hass.states.async_set(INSIDE, change["inside"])
    await driver.cycle(change.get("seconds", 10), **change.get("data", PUMP_ON))

    assert lines(caplog, END) == [f"circulation end ({reason})"]
    assert "(circulation)" not in driver.line
    # The circulation's setpoint 20 and heater ON are replaced by the normal OFF decision: setpoint 0 and heater off
    assert driver.published_values("selflowtemp") == [0]
    assert driver.published_values("heatingoff") == ["on"]
    released = len(lines(caplog, RELEASED))
    if reason == "flame on":
        assert "circulation: the boiler fired during circulation" in warnings(caplog)

    # Heater OFF read back: released once, nothing more sent for it
    for _ in range(3):
        await driver.cycle(10, **PUMP_ON)
        if len(lines(caplog, RELEASED)) > released:
            break

    assert len(lines(caplog, RELEASED)) == released + 1
    assert driver.published_values("selflowtemp") == []
    assert driver.published_values("heatingoff") == []


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*cold_room({}, **{CONF_RADIATORS: [RADIATOR]}))
async def test_the_ending_cycle_after_the_valves_close(hass, entry, driver, caplog):
    hass.states.async_set(RADIATOR, "heat", {"hvac_action": "heating", "hvac_modes": ["heat", "off"]})
    await circulate(driver)
    assert lines(caplog, START)

    closing(hass)
    await hass.async_block_till_done()
    await driver.cycle(10, **PUMP_ON)

    assert lines(caplog, END) == ["circulation end (valves closed)"]
    assert driver.published_values("selflowtemp") == [0]
    assert driver.published_values("heatingoff") == ["on"]

    released = len(lines(caplog, RELEASED))
    for _ in range(3):
        await driver.cycle(10, **PUMP_ON)
        if len(lines(caplog, RELEASED)) > released:
            break

    assert len(lines(caplog, RELEASED)) == released + 1


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*cold_room())
async def test_a_normal_decision_that_turns_on_ends_circulation_with_heater_on(hass, entry, driver, caplog):
    await circulate(driver)
    await driver.cycle(10, **PUMP_ON)
    assert lines(caplog, END) == []

    # A cold outside raises the calculated setpoint (low-pass filtered) over the cold setpoint: the normal decision becomes heater ON
    hass.states.async_set(OUTSIDE, "-10")
    for _ in range(4):
        await driver.cycle(5, **PUMP_ON)
        if lines(caplog, END):
            break

    assert lines(caplog, END) == ["circulation end (heater on)"]

    # The ending cycle's own decision (an auto-run loop may have taken it): the normal one, heater ON; the next cycle shows it
    await driver.cycle(5, **PUMP_ON)
    assert "(circulation)" not in driver.line
    assert "heater=ON" in driver.line and "(normal)" in driver.line
    # Circulation never requested OFF itself: since the start no cycle asked for heater OFF
    after_start = [m for m in lines(caplog, "cycle ") if "circulation" in m or "(normal)" in m]
    assert not [m for m in after_start if "heater=OFF" in m]
    assert 0 not in driver.published_values("selflowtemp")
    assert "heatingoff" not in [name for name, _ in driver.sent] or driver.published_values("heatingoff") == []
    assert warnings(caplog) == []


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*cold_room({CONF_CIRCULATION_DURATION: 240}))
async def test_a_long_run_ends_by_its_own_duration(entry, driver, caplog):
    await circulate(driver)
    start = lines(caplog, START)
    assert len(start) == 1

    await driver.cycle(100, **PUMP_ON)
    await driver.cycle(100, **PUMP_ON)
    # About 215 s after the start, 235 s after the flame off: still running
    assert lines(caplog, END) == []
    await driver.cycle(70, **PUMP_ON)
    # Past the window of 300 s since the burn: the duration of 240 s has now elapsed as well
    assert lines(caplog, END) == ["circulation end (duration)"]
    assert len(lines(caplog, START)) == 1


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*cold_room({}))
async def test_a_late_start_is_not_made(hass, entry, climate, coordinator, driver, caplog):
    await driver.start()
    # Not cold enough for heat: the room is warm, no demand
    hass.states.async_set(INSIDE, "17")
    await driver.cycle(**PUMP)
    await driver.cycle(**DRAW)
    await driver.cycle(5, **FLAME_OFF | PUMP_ON)
    await driver.cycle(5, **PUMP_OFF)
    assert coordinator.awaiting_pump_restart
    assert lines(caplog, START) == []

    # Long after the draw the demand appears; the wait is still armed
    await driver.cycle(1000, **PUMP_OFF)
    hass.states.async_set(INSIDE, "15")
    await driver.cycle(10, **PUMP_OFF)

    assert coordinator.awaiting_pump_restart
    assert lines(caplog, START) == []
    assert "(circulation)" not in driver.line


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*cold_room({}))
async def test_pwm_stays_idle_through_a_circulation_without_pwm(entry, climate, driver, caplog):
    await circulate(driver)

    assert lines(caplog, START)
    assert climate.pwm.status == PWMStatus.IDLE
    assert not [m for m in lines(caplog, "pwm ") if m.startswith("pwm ON")]
