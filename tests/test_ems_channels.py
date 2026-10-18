"""EMS-ESP channel scenarios against the real boiler capture."""
from custom_components.sat.channels import (
    RELEASE,
    ChannelStatus,
    DesiredState,
    Quantity,
)
from custom_components.sat.coordinator import DeviceState
from tests.const import EMS_BOILER_DATA
from tests.test_mqtt_ems import command, cycle, status


async def test_boiler_reverting_the_setpoint_is_drift_and_rewritten(ems, boiler_data, published, freezer):
    desired = DesiredState(control_setpoint=40)
    await boiler_data(**EMS_BOILER_DATA)
    await cycle(ems, desired)

    freezer.tick(1)
    await boiler_data(selflowtemp=40)
    await cycle(ems, desired)
    assert status(ems, Quantity.CONTROL_SETPOINT) == ChannelStatus.IN_SYNC

    freezer.tick(1)
    await boiler_data(selflowtemp=50)
    await cycle(ems, desired)

    state = ems._channels.states[Quantity.CONTROL_SETPOINT]
    assert (state.status, state.drift_count) == (ChannelStatus.DRIFTED, 1)
    assert published() == [command('{"cmd": "selflowtemp", "value": 40}')] * 3


async def test_setpoint_above_the_boiler_knob_stays_out_of_sync_and_is_written_every_cycle(ems, boiler_data, published, freezer):
    desired = DesiredState(control_setpoint=60)
    await boiler_data(**EMS_BOILER_DATA)
    await cycle(ems, desired)

    for _ in range(3):
        freezer.tick(1)
        await boiler_data(selflowtemp=50)
        await cycle(ems, desired)
        assert status(ems, Quantity.CONTROL_SETPOINT) == ChannelStatus.OUT_OF_SYNC

    assert published() == [command('{"cmd": "selflowtemp", "value": 60}')] * 4


async def test_enabled_idle_boiler_needs_no_heater_write_and_off_is_written_once(ems, boiler_data, published, freezer):
    await boiler_data(**EMS_BOILER_DATA)

    await cycle(ems, DesiredState(heater_state=DeviceState.ON))
    assert published() == []
    assert status(ems, Quantity.HEATER_STATE) == ChannelStatus.IN_SYNC

    await cycle(ems, DesiredState(heater_state=DeviceState.OFF))
    assert published() == [command('{"cmd": "heatingoff", "value": "on"}')]

    freezer.tick(1)
    await boiler_data(heatingoff="on")
    await cycle(ems, DesiredState(heater_state=DeviceState.OFF))
    assert len(published()) == 1
    assert status(ems, Quantity.HEATER_STATE) == ChannelStatus.IN_SYNC


async def test_released_setpoint_is_not_written_in_off_whatever_it_reads_back(ems, boiler_data, published, freezer):
    await boiler_data(**EMS_BOILER_DATA)
    await cycle(ems, DesiredState(control_setpoint=40))

    for value in (0, 40, 55):
        freezer.tick(1)
        await boiler_data(selflowtemp=value)
        await cycle(ems, DesiredState(control_setpoint=RELEASE))

    assert len(published()) == 1
    assert status(ems, Quantity.CONTROL_SETPOINT) == ChannelStatus.IDLE
