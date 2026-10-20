"""Tests for the boiler temperature wait of `OvershootProtection`."""
import asyncio
import logging
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.sat.channels import DesiredState
from custom_components.sat.const import MAXIMUM_RELATIVE_MODULATION, MINIMUM_SETPOINT
from custom_components.sat.coordinator import DeviceState
from custom_components.sat.overshoot_protection import OvershootProtection

WAITING = "Waiting for boiler temperature"


@pytest.fixture
def coordinator() -> MagicMock:
    mock = MagicMock(maximum_setpoint_value=60, boiler_temperature=None, relative_modulation_value=0)
    mock.async_submit = AsyncMock()
    mock.async_control_heating_loop = AsyncMock()
    return mock


@pytest.fixture(autouse=True)
def fast_sleep() -> Iterator[None]:
    with patch("custom_components.sat.overshoot_protection.SLEEP_INTERVAL", 0.01):
        yield


def waiting_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.getMessage() == WAITING]


def expected() -> DesiredState:
    return DesiredState(control_setpoint=60, maximum_relative_modulation=MAXIMUM_RELATIVE_MODULATION, heater_state=DeviceState.ON)


@pytest.mark.parametrize("missing", [None, 0.0])
async def test_wait_yields_and_can_time_out(coordinator, missing):
    coordinator.boiler_temperature = missing
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(OvershootProtection(coordinator, "radiators")._wait_for_stable_temperature(), timeout=0.1)
    task.cancel()

    assert ticks > 1


async def test_each_pass_submits_the_calibration_state_and_runs_the_loop(coordinator):
    overshoot = OvershootProtection(coordinator, "radiators")
    passes = 0

    async def control_loop() -> None:
        nonlocal passes
        passes += 1
        if passes == 3:
            raise asyncio.CancelledError

    coordinator.async_control_heating_loop.side_effect = control_loop

    with pytest.raises(asyncio.CancelledError):
        await overshoot._wait_for_stable_temperature()

    assert coordinator.async_submit.await_args_list == [((expected(),),)] * 3
    assert coordinator.async_control_heating_loop.await_count == 3


async def test_logs_once_when_the_temperature_is_missing(coordinator: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    overshoot = OvershootProtection(coordinator, "radiators")
    passes = 0

    async def control_loop() -> None:
        nonlocal passes
        passes += 1
        if passes == 2:
            raise asyncio.CancelledError

    coordinator.async_control_heating_loop.side_effect = control_loop

    with caplog.at_level(logging.DEBUG), pytest.raises(asyncio.CancelledError):
        await overshoot._wait_for_stable_temperature()

    assert waiting_logs(caplog) == [WAITING]


async def test_no_log_when_the_temperature_is_available(coordinator: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    coordinator.boiler_temperature = 30.0
    coordinator.async_submit.side_effect = asyncio.CancelledError

    with caplog.at_level(logging.DEBUG), pytest.raises(asyncio.CancelledError):
        await OvershootProtection(coordinator, "radiators")._wait_for_stable_temperature()

    assert waiting_logs(caplog) == []


@pytest.mark.parametrize("missing", [None, 0.0])
async def test_calibration_continues_once_the_temperature_arrives(coordinator, missing):
    coordinator.boiler_temperature = missing
    overshoot = OvershootProtection(coordinator, "radiators")
    submitted: list[DesiredState] = []

    async def submit(state: DesiredState) -> None:
        submitted.append(state)
        if len(submitted) == 2:
            coordinator.boiler_temperature = 30.0
        if len(submitted) == 3:
            raise asyncio.CancelledError

    coordinator.async_submit.side_effect = submit

    with pytest.raises(asyncio.CancelledError):
        await overshoot._wait_for_stable_temperature()

    # Two waiting passes with the calibration setpoint, then the stable phase follows the boiler temperature
    assert submitted[:2] == [expected()] * 2
    assert submitted[2].control_setpoint == 30.0


RESET = DesiredState(control_setpoint=MINIMUM_SETPOINT, heater_state=DeviceState.OFF)


async def test_calculate_returns_the_overshoot_without_a_reset(coordinator: MagicMock) -> None:
    coordinator.flame_active = True
    overshoot = OvershootProtection(coordinator, "radiators")
    overshoot._stable_temperature = 42.0

    with patch.object(overshoot, "_wait_for_stable_temperature", new=AsyncMock()):
        assert await overshoot.calculate() == 42.0

    coordinator.async_submit.assert_not_awaited()


@pytest.mark.parametrize("phase", ["flame", "stable"])
async def test_calculate_resets_the_boiler_on_timeout(coordinator: MagicMock, phase: str) -> None:
    coordinator.flame_active = phase == "stable"
    with patch("custom_components.sat.overshoot_protection.OVERSHOOT_PROTECTION_INITIAL_WAIT", 0.05), patch("custom_components.sat.overshoot_protection.OVERSHOOT_PROTECTION_STABLE_WAIT", 0.05):
        with pytest.raises(TimeoutError):
            await OvershootProtection(coordinator, "radiators").calculate()

    assert [call.args[0] for call in coordinator.async_submit.await_args_list].count(RESET) == 1
    assert coordinator.async_submit.await_args_list[-1].args[0] == RESET


@pytest.mark.parametrize("error", [asyncio.CancelledError(), TypeError("boom")])
async def test_calculate_resets_once_and_reraises(coordinator: MagicMock, error: BaseException) -> None:
    overshoot = OvershootProtection(coordinator, "radiators")

    with patch.object(overshoot, "_wait_for_flame", new=AsyncMock(side_effect=error)), pytest.raises(type(error)) as raised:
        await overshoot.calculate()

    assert raised.value is error
    coordinator.async_submit.assert_awaited_once_with(RESET)
