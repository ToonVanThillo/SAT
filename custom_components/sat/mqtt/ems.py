from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..channels import ChannelDefinition, Channels, Quantity
from ..const import (
    CONF_HOT_WATER_BURNER_POWER,
    CONF_HOT_WATER_FLOW_TEMPERATURE,
    CONF_MAXIMUM_RELATIVE_MODULATION,
    CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR,
    OPTIONS_DEFAULTS,
)
from ..coordinator import BoilerSnapshot, DeviceState, Reading
from ..flame import HotWaterSignal
from ..helpers import float_value
from . import SatMqttCoordinator

DATA_ON = "on"
DATA_OFF = "off"

DATA_BOILER_DATA = "boiler_data"
DATA_FLAME_ACTIVE = "burngas"
DATA_CONTROL_SETPOINT = "selflowtemp"
DATA_REL_MOD_LEVEL = "curburnpow"
DATA_BOILER_TEMPERATURE = "curflowtemp"
DATA_RETURN_TEMPERATURE = "rettemp"

DATA_TAP_WATER_ACTIVE = "tapwateractive"
DATA_CENTRAL_HEATING = "heatingactive"
DATA_HEATING_PUMP = "heatingpump"
DATA_SELECTED_BURNER_POWER = "selburnpow"
DATA_SERVICE_CODE = "servicecode"
DATA_BOILER_CAPACITY = "nompower"

DATA_REL_MIN_MOD_LEVEL = "burnminpower"
DATA_MAX_REL_MOD_LEVEL_SETTING = "burnmaxpower"
DATA_HEATING_OFF = "heatingoff"
DATA_MAX_SETPOINT = "heatingtemp"

_LOGGER: logging.Logger = logging.getLogger(__name__)


def _reading(snapshot: BoilerSnapshot, key: str, convert: Callable[[Any], Any]) -> Reading | None:
    """Read `key` from the snapshot converted to SAT terms, keeping its receive time; None if missing or not convertible."""
    if (reading := snapshot.readings.get(key)) is None or (value := convert(reading.value)) is None:
        return None

    return Reading(value=value, received_at=reading.received_at)


def _encode_control_setpoint(value: float) -> int:
    # EMS-ESP takes whole degrees; 10 (the minimum setpoint) is sent as 0.
    setpoint = round(value)
    return 0 if setpoint == 10 else setpoint


class SatEmsMqttCoordinator(SatMqttCoordinator):
    """Class to manage to fetch data from the OTGW Gateway using MQTT."""

    traced_keys: ClassVar[frozenset[str]] = frozenset({
        DATA_HEATING_PUMP, DATA_CENTRAL_HEATING, DATA_TAP_WATER_ACTIVE, DATA_FLAME_ACTIVE,
        DATA_CONTROL_SETPOINT, DATA_SELECTED_BURNER_POWER, DATA_SERVICE_CODE, DATA_HEATING_OFF,
    })

    def __init__(self, hass: HomeAssistant, device_id: str, config_data: Mapping[str, Any], options: Mapping[str, Any] | None = None, *, config_entry: ConfigEntry | None) -> None:
        super().__init__(hass, device_id, config_data, options, config_entry=config_entry)

        floor = int(self._options.get(CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR, OPTIONS_DEFAULTS[CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR]))
        heater_states = {DATA_OFF: DeviceState.ON, DATA_ON: DeviceState.OFF}

        # Hot-water signal values from the boiler's configuration; None disables the signal
        self._hot_water_burner_power: float | None = float_value(self._options.get(CONF_HOT_WATER_BURNER_POWER))
        self._hot_water_flow_temperature: float | None = float_value(self._options.get(CONF_HOT_WATER_FLOW_TEMPERATURE))
        # The control setpoints being published, one entry per write in progress; the channel records a value as sent only after
        # the wait that follows its publish
        self._control_setpoints_publishing: list[int] = []

        # The highest max relative modulation SAT writes: the configured maximum, or the floor it raises lower values to.
        # At or above it, the burner power is no hot-water signal; the coordinator warns when it starts.
        self._maximum_relative_modulation_written: float = max(float(self._options.get(CONF_MAXIMUM_RELATIVE_MODULATION, OPTIONS_DEFAULTS[CONF_MAXIMUM_RELATIVE_MODULATION])), floor)
        if self._hot_water_burner_power is not None and self._maximum_relative_modulation_written >= self._hot_water_burner_power:
            self._hot_water_burner_power = None

        self._channels = Channels([
            # While heating is enabled, EMS-ESP falls back to the panel value about a minute after the last write; under `heatingoff` it sends 0 itself.
            ChannelDefinition(
                quantity=Quantity.CONTROL_SETPOINT,
                read=lambda snapshot: _reading(snapshot, DATA_CONTROL_SETPOINT, float_value),
                encode=_encode_control_setpoint,
                write=self._async_write_control_setpoint,
                write_every_cycle=True,
            ),
            ChannelDefinition(
                quantity=Quantity.MAXIMUM_RELATIVE_MODULATION,
                read=lambda snapshot: _reading(snapshot, DATA_MAX_REL_MOD_LEVEL_SETTING, float_value),
                encode=lambda value: max(round(value), floor),
                write=lambda value: self._async_write_command(DATA_MAX_REL_MOD_LEVEL_SETTING, value),
            ),
            ChannelDefinition(
                quantity=Quantity.HEATER_STATE,
                read=lambda snapshot: _reading(snapshot, DATA_HEATING_OFF, heater_states.get),
                write=lambda state: self._async_write_command(DATA_HEATING_OFF, DATA_OFF if state == DeviceState.ON else DATA_ON),
            ),
            ChannelDefinition(
                quantity=Quantity.MAXIMUM_SETPOINT,
                read=lambda snapshot: _reading(snapshot, DATA_MAX_SETPOINT, float_value),
                encode=round,
                write=lambda value: self._async_write_command(DATA_MAX_SETPOINT, value),
            ),
        ])

    @property
    def device_type(self) -> str:
        return "Energy Management System (via mqtt)"

    @property
    def supports_setpoint_management(self) -> bool:
        return True

    @property
    def supports_maximum_setpoint_management(self) -> bool:
        return True

    @property
    def supports_relative_modulation(self) -> bool:
        return True

    @property
    def releases_control_setpoint_when_heater_off(self) -> bool:
        # `heatingoff` keeps the boiler off, so the setpoint isn't needed.
        return True

    @property
    def relative_modulation_limits_hot_water(self) -> bool:
        # EMS-ESP limits hot water power with `dhw.maxpower`, not `burnmaxpower`.
        return False

    @property
    def device_active(self) -> bool:
        return self.data.get(DATA_CENTRAL_HEATING) == DATA_ON

    @property
    def flame_active(self) -> bool:
        return self.data.get(DATA_FLAME_ACTIVE) == DATA_ON

    @property
    def hot_water_active(self) -> bool:
        return self.data.get(DATA_TAP_WATER_ACTIVE) == DATA_ON

    @property
    def setpoint(self) -> Optional[float]:
        return float_value(self.data.get(DATA_CONTROL_SETPOINT))

    @property
    def boiler_temperature(self) -> Optional[float]:
        return float_value(self.data.get(DATA_BOILER_TEMPERATURE))

    @property
    def return_temperature(self) -> Optional[float]:
        return float_value(self.data.get(DATA_RETURN_TEMPERATURE))

    @property
    def relative_modulation_value(self) -> Optional[float]:
        return float_value(self.data.get(DATA_REL_MOD_LEVEL))

    @property
    def boiler_capacity(self) -> Optional[float]:
        return float_value(self.data.get(DATA_BOILER_CAPACITY))

    @property
    def minimum_relative_modulation_value(self) -> Optional[float]:
        return float_value(self.data.get(DATA_REL_MIN_MOD_LEVEL))

    @property
    def maximum_relative_modulation_value(self) -> Optional[float]:
        return float_value(self.data.get(DATA_MAX_REL_MOD_LEVEL_SETTING))

    @property
    def member_id(self) -> Optional[int]:
        # Not supported (yet)
        return None

    async def boot(self) -> None:
        """Warn that the configured hot water burner power is not used, if SAT may write it itself.

        Runs once, when the coordinator of the config entry starts; temporary coordinators of the config flow are never started.
        """
        if (burner_power := float_value(self._options.get(CONF_HOT_WATER_BURNER_POWER))) is not None and self._hot_water_burner_power is None:
            _LOGGER.warning(
                "The hot water burner power (%g%%) is not used to detect hot water: SAT may write a maximum relative modulation of %g%% (the configured maximum or floor), so heating can run at that power too",
                burner_power, self._maximum_relative_modulation_written,
            )

    def get_tracked_entities(self) -> list[str]:
        return [DATA_BOILER_DATA]

    def _live_flame_active(self) -> bool | None:
        return None if (value := self.data.live_value(DATA_FLAME_ACTIVE)) is None else value == DATA_ON

    def _live_hot_water_active(self) -> bool:
        return self.data.live_value(DATA_TAP_WATER_ACTIVE) == DATA_ON

    def _hot_water_signals(self) -> frozenset[HotWaterSignal]:
        """The hot-water signals in the live boiler data: `tapwateractive` on; `selburnpow` at the configured hot water burner power;
        `selflowtemp` at the configured hot water flow temperature while that is not the control setpoint SAT last sent or is sending."""
        signals: set[HotWaterSignal] = set()

        if self._live_hot_water_active():
            signals.add(HotWaterSignal.FLAG)

        if self._hot_water_burner_power is not None and float_value(self.data.live_value(DATA_SELECTED_BURNER_POWER)) == self._hot_water_burner_power:
            signals.add(HotWaterSignal.BURNER_POWER)

        if self._hot_water_flow_temperature is not None:
            flow_temperature = float_value(self.data.live_value(DATA_CONTROL_SETPOINT))
            sent = (self.channel_states[Quantity.CONTROL_SETPOINT].sent, *self._control_setpoints_publishing)
            if flow_temperature == self._hot_water_flow_temperature and flow_temperature not in sent:
                signals.add(HotWaterSignal.FLOW_TEMPERATURE)

        return frozenset(signals)

    async def _async_write_control_setpoint(self, value: int) -> None:
        """Publish the control setpoint; raises if publishing fails."""
        self._control_setpoints_publishing.append(value)
        try:
            await self._async_write_command(DATA_CONTROL_SETPOINT, value)
        finally:
            self._control_setpoints_publishing.remove(value)

    async def _async_write_command(self, command: str, value: Any) -> None:
        """Publish an EMS-ESP boiler command; raises if publishing fails."""
        await self._async_publish(json.dumps({"cmd": command, "value": value}))

    def _get_topic_for_subscription(self, key: str) -> str:
        return f"{self._topic}/{key}"

    def _get_topic_for_publishing(self) -> str:
        return f"{self._topic}/boiler"

    def _process_message_payload(self, key: str, value: str):
        try:
            self.async_set_updated_data(json.loads(value))
        except json.JSONDecodeError as error:
            _LOGGER.error("Failed to decode JSON payload: %s. Error: %s", value, error)
