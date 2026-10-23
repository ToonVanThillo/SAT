from __future__ import annotations

import logging
from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping, Any, Optional, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback, HassJob
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .boiler import BoilerTemperatureTracker, BoilerState, STABILIZATION_MARGIN
from .channels import RELEASE, Channels, ChannelState, ChannelStatus, DesiredState, Quantity
from .const import *
from .flame import Flame, FlameState
from .helpers import calculate_default_maximum_setpoint, seconds_since
from .manufacturer import Manufacturer, ManufacturerFactory
from .manufacturers.geminox import Geminox
from .manufacturers.ideal import Ideal
from .manufacturers.intergas import Intergas
from .manufacturers.nefit import Nefit
from .pwm import PWMState

if TYPE_CHECKING:
    from .climate import SatClimate

_LOGGER: logging.Logger = logging.getLogger(__name__)


class DeviceState(str, Enum):
    ON = "on"
    OFF = "off"


@dataclass(frozen=True, slots=True, kw_only=True)
class Reading:
    """A raw device value with the time it was received; a value restored from storage has no receive time."""
    value: Any
    received_at: datetime | None

    @property
    def restored(self) -> bool:
        return self.received_at is None


@dataclass(frozen=True, slots=True, kw_only=True)
class BoilerSnapshot:
    """Immutable view of the boiler that the control logic reads during one cycle."""
    boiler: BoilerState
    flame: FlameState
    maximum_relative_modulation_value: float | None
    manufacturer: Manufacturer | None
    readings: Mapping[str, Reading]


class SatData(dict):
    def __init__(self) -> None:
        super().__init__()
        self._is_dirty: bool = False
        self._received_at: dict[str, datetime | None] = {}

    def __setitem__(self, key, value):
        self.update({key: value})

    def update(self, other: Mapping[str, Any], *, restored: bool = False) -> None:
        """Store the values, stamped with the current time, or without a time when they are restored from storage."""
        received_at = None if restored else dt_util.utcnow()

        for key, value in other.items():
            if self.get(key) != value:
                self._is_dirty = True

            super().__setitem__(key, value)
            self._received_at[key] = received_at

    def __delitem__(self, key):
        self._is_dirty = True
        super().__delitem__(key)
        self._received_at.pop(key, None)

    def readings(self) -> Mapping[str, Reading]:
        """Return a read-only copy of the current values with their receive times."""
        return MappingProxyType({key: Reading(value=value, received_at=self._received_at.get(key)) for key, value in self.items()})

    def reset_dirty(self):
        self._is_dirty = False

    def is_dirty(self) -> bool:
        return self._is_dirty


class SatDataUpdateCoordinatorFactory:
    @staticmethod
    def resolve(hass: HomeAssistant, mode: str, device: str, data: Mapping[str, Any], options: Mapping[str, Any] | None = None, *, config_entry: ConfigEntry | None) -> SatDataUpdateCoordinator:
        if mode == MODE_FAKE:
            from .fake import SatFakeCoordinator
            return SatFakeCoordinator(hass=hass, config_data=data, options=options, config_entry=config_entry)

        if mode == MODE_SIMULATOR:
            from .simulator import SatSimulatorCoordinator
            return SatSimulatorCoordinator(hass=hass, config_data=data, options=options, config_entry=config_entry)

        if mode == MODE_SWITCH:
            from .switch import SatSwitchCoordinator
            return SatSwitchCoordinator(hass=hass, entity_id=device, config_data=data, options=options, config_entry=config_entry)

        if mode == MODE_ESPHOME:
            from .esphome import SatEspHomeCoordinator
            return SatEspHomeCoordinator(hass=hass, device_id=device, config_data=data, options=options, config_entry=config_entry)

        if mode == MODE_MQTT_EMS:
            from .mqtt.ems import SatEmsMqttCoordinator
            return SatEmsMqttCoordinator(hass=hass, device_id=device, config_data=data, options=options, config_entry=config_entry)

        if mode == MODE_MQTT_OPENTHERM:
            from .mqtt.opentherm import SatOpenThermMqttCoordinator
            return SatOpenThermMqttCoordinator(hass=hass, device_id=device, config_data=data, options=options, config_entry=config_entry)

        if mode == MODE_SERIAL:
            from .serial import SatSerialCoordinator
            return SatSerialCoordinator(hass=hass, port=device, config_data=data, options=options, config_entry=config_entry)

        raise Exception(f'Invalid mode[{mode}]')


class SatDataUpdateCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, config_data: Mapping[str, Any], options: Mapping[str, Any] | None = None, *, config_entry: ConfigEntry | None) -> None:
        """Initialize."""
        super().__init__(hass, _LOGGER, config_entry=config_entry, name=DOMAIN)
        self.data: SatData = SatData()

        self._boiler_temperature_cold: Optional[float] = None
        self._boiler_temperatures: list[tuple[float, float]] = []
        self._boiler_temperature_tracker: BoilerTemperatureTracker = BoilerTemperatureTracker()

        self._flame: Flame = Flame()
        self._snapshot: BoilerSnapshot | None = None
        # Set by backends migrated to channels; the others keep the `async_set_*` path.
        self._channels: Channels | None = None
        self._device_on_since: Optional[float] = None
        self._listeners_unsub: Optional[Callable[[], None]] = None
        self._started: bool = False
        self._torn_down: bool = False

        self._options: Mapping[str, Any] = options or {}
        self._config_data: Mapping[str, Any] = config_data

        self._manufacturer: Optional[Manufacturer] = None
        self._simulation: bool = bool(self._options.get(CONF_SIMULATION))
        self._heating_system: str = str(config_data.get(CONF_HEATING_SYSTEM, HEATING_SYSTEM_UNKNOWN))

        if config_data.get(CONF_MANUFACTURER) is not None:
            self._manufacturer = ManufacturerFactory.resolve_by_name(config_data.get(CONF_MANUFACTURER))

        self._flame_listener_unsub: Callable[[], None] | None = self.async_add_listener(lambda: self._flame.update(boiler_state=self.boiler))

    @property
    @abstractmethod
    def device_id(self) -> str:
        pass

    @property
    @abstractmethod
    def device_type(self) -> str:
        pass

    @property
    def device_status(self) -> BoilerStatus:
        """Return the current status of the device."""
        if self.boiler_temperature is None:
            return BoilerStatus.INSUFFICIENT_DATA

        if self.hot_water_active:
            return BoilerStatus.HOT_WATER

        if self.setpoint is None or self.setpoint <= MINIMUM_SETPOINT:
            return BoilerStatus.IDLE

        if self.device_active:
            if self.boiler_temperature_cold is not None and self.boiler_temperature_cold > self.boiler_temperature:
                if self.boiler_temperature_derivative is not None and self.boiler_temperature_derivative <= 0:
                    return BoilerStatus.PUMP_STARTING

                if self._boiler_temperature_tracker.active and self.setpoint > self.boiler_temperature:
                    return BoilerStatus.PREHEATING

        # We assume space-heating mode with a valid setpoint.
        delta = self.boiler_temperature - self.setpoint

        # Below the setpoint
        if delta < -BOILER_DEADBAND:
            if self.flame_active:
                if self._boiler_temperature_tracker.active:
                    return BoilerStatus.HEATING_UP

                return BoilerStatus.OVERSHOOT_HANDLING
            else:
                return BoilerStatus.WAITING_FOR_FLAME

        # Near/at the setpoint
        if abs(delta) <= BOILER_DEADBAND:
            return BoilerStatus.AT_SETPOINT

        # Above the setpoint
        if delta > BOILER_DEADBAND:
            if self.flame_active:
                if self._boiler_temperature_tracker.active:
                    # Way above setpoint -> cooling down.
                    if delta > STABILIZATION_MARGIN:
                        return BoilerStatus.COOLING_DOWN

                    # Slightly above the setpoint while still burning -> near setpoint / stabilizing.
                    return BoilerStatus.NEAR_SETPOINT

                # Flame still on, but tracker inactive. We are over setpoint -> overshoot handling.
                return BoilerStatus.OVERSHOOT_HANDLING

            # Most modern boilers are in anti-cycling here.
            return BoilerStatus.ANTI_CYCLING

        return BoilerStatus.INSUFFICIENT_DATA

    @property
    def boiler(self) -> BoilerState:
        return BoilerState(
            flame_active=self.flame_active,
            hot_water_active=self.hot_water_active,

            status=self.device_status,
            is_active=self.device_active,
            is_inactive=not self.device_active,

            setpoint=self.setpoint,
            flow_temperature=self.boiler_temperature,
            return_temperature=self.return_temperature,
            relative_modulation_level=self.relative_modulation_value,
        )

    @property
    def flame(self) -> FlameState:
        return FlameState(
            is_active=self._flame.is_active,
            is_inactive=self._flame.is_inactive,
            health_status=self._flame.health_status,

            latest_on_time_seconds=self._flame.latest_on_time_seconds,
            average_on_time_seconds=self._flame.average_on_time_seconds,
            last_cycle_duration_seconds=self._flame.last_cycle_duration_seconds,

            sample_count_4h=self._flame.sample_count_4h,
            cycles_last_hour=self._flame.cycles_last_hour,
            duty_ratio_last_15m=self._flame.duty_ratio_last_15m,
            median_on_duration_seconds_4h=self._flame.median_on_duration_seconds_4h,
        )

    @property
    def manufacturer(self) -> Optional[Manufacturer]:
        return self._manufacturer

    @property
    @abstractmethod
    def setpoint(self) -> Optional[float]:
        pass

    @property
    @abstractmethod
    def device_active(self) -> bool:
        pass

    @property
    @abstractmethod
    def member_id(self) -> Optional[int]:
        pass

    @property
    def flame_active(self) -> bool:
        return self.device_active

    @property
    def heater_on_since(self) -> Optional[float]:
        return self._device_on_since

    @property
    def hot_water_active(self) -> bool:
        return False

    @property
    def hot_water_setpoint(self) -> Optional[float]:
        return None

    @property
    def boiler_temperature(self) -> Optional[float]:
        return None

    @property
    def return_temperature(self) -> Optional[float]:
        return None

    @property
    def boiler_temperature_filtered(self) -> Optional[float]:
        # Not able to use if we do not have at least two values
        if len(self._boiler_temperatures) < 2:
            return self.boiler_temperature

        # Some noise filtering on the boiler temperature
        difference_boiler_temperature_sum = sum(
            abs(j[1] - i[1]) for i, j in zip(self._boiler_temperatures, self._boiler_temperatures[1:])
        )

        # Average it and return it
        return round(difference_boiler_temperature_sum / (len(self._boiler_temperatures) - 1), 2)

    @property
    def boiler_temperature_derivative(self) -> Optional[float]:
        if len(self._boiler_temperatures) <= 1:
            return None

        first_time, first_temperature = self._boiler_temperatures[-2]
        last_time, last_temperature = self._boiler_temperatures[-1]

        time_delta = last_time - first_time
        if time_delta <= 0:
            return None

        return round((last_temperature - first_temperature) / time_delta, 2)

    @property
    def boiler_temperature_cold(self) -> Optional[float]:
        return self._boiler_temperature_cold

    @property
    def boiler_temperature_tracking(self) -> bool:
        return self._boiler_temperature_tracker.active

    @property
    def minimum_hot_water_setpoint(self) -> float:
        return 30

    @property
    def maximum_hot_water_setpoint(self) -> float:
        return 60

    @property
    def relative_modulation_value(self) -> Optional[float]:
        return None

    @property
    def maximum_setpoint_value(self) -> Optional[float]:
        return self.maximum_setpoint

    @property
    def boiler_capacity(self) -> Optional[float]:
        return None

    @property
    def minimum_boiler_capacity(self) -> Optional[float]:
        if (minimum_relative_modulation_value := self.minimum_relative_modulation_value) is None:
            return None

        if (boiler_capacity := self.boiler_capacity) is None:
            return None

        if boiler_capacity == 0:
            return 0

        return boiler_capacity * (minimum_relative_modulation_value / 100)

    @property
    def boiler_power(self) -> Optional[float]:
        if (boiler_capacity := self.boiler_capacity) is None:
            return None

        if (minimum_boiler_capacity := self.minimum_boiler_capacity) is None:
            return None

        if (relative_modulation_value := self.relative_modulation_value) is None:
            return None

        if not self.flame_active:
            return 0

        return minimum_boiler_capacity + ((boiler_capacity - minimum_boiler_capacity) * (relative_modulation_value / 100))

    @property
    def minimum_relative_modulation_value(self) -> Optional[float]:
        return None

    @property
    def maximum_relative_modulation_value(self) -> Optional[float]:
        return None

    @property
    def minimum_setpoint(self) -> float:
        """Return the minimum setpoint temperature before the device starts to overshoot."""
        return float(self._config_data.get(CONF_MINIMUM_SETPOINT))

    @property
    def maximum_setpoint(self) -> float:
        """Return the maximum setpoint temperature that the device can support."""
        default_maximum_setpoint = calculate_default_maximum_setpoint(self._heating_system)
        return float(self._options.get(CONF_MAXIMUM_SETPOINT, default_maximum_setpoint))

    @property
    def supports_setpoint_management(self):
        """Returns whether the device supports setting a boiler setpoint.

        This property is used to determine whether the coordinator can send a setpoint to the device.
        If a device doesn't support setpoint management, the coordinator won't be able to control the temperature.
        """
        return False

    @property
    def supports_hot_water_setpoint_management(self):
        """Returns whether the device supports setting a hot water setpoint.

        This property is used to determine whether the coordinator can send a setpoint to the device.
        If a device doesn't support setpoint management, the coordinator won't be able to control the temperature.
        """
        return False

    @property
    def supports_relative_modulation(self):
        """Returns whether the device supports having relative modulation value.

        This property is used to determine whether the coordinator can retrieve the relative modulation value from the device.
        If a device doesn't support the relative modulation value, the coordinator won't be able to retrieve the value.
        """
        if isinstance(self.manufacturer, (Ideal, Intergas, Geminox, Nefit)):
            return False

        return True

    @property
    def supports_relative_modulation_management(self):
        """Returns whether the device supports setting a relative modulation value.

        This property is used to determine whether the coordinator can send a relative modulation value to the device.
        If a device doesn't support relative modulation management, the coordinator won't be able to control the value.
        """
        return True

    @property
    def relative_modulation_limits_hot_water(self) -> bool:
        """Returns whether the maximum relative modulation also limits the hot water power.

        If it doesn't, the relative modulation ignores hot water, so a tap draw doesn't change the requested value.
        """
        return True

    @property
    def releases_control_setpoint_when_heater_off(self) -> bool:
        """Returns whether the control setpoint is released, instead of set to the minimum, while the heater is requested OFF.

        Only for backends that switch heating off with a separate command; a backend whose boiler is kept off by a low setpoint must keep receiving it.
        """
        return False

    @property
    def supports_maximum_setpoint_management(self):
        """Returns whether the device supports setting a maximum setpoint.

        This property is used to determine whether the coordinator can send a maximum setpoint to the device.
        If a device doesn't support maximum setpoint management, the coordinator won't be able to control the value.
        """
        return False

    async def async_setup(self) -> None:
        """Perform setup when the integration is about to be added to Home Assistant."""
        pass

    async def async_added_to_hass(self) -> None:
        """Start the coordinator: the backend's start, then request the maximum setpoint.

        Runs once: does nothing if already started (e.g. the climate re-added on an entity ID
        change) or torn down. A reload creates a new coordinator. A failing backend start is
        logged and skips the rest.
        """
        if self._started or self._torn_down:
            return

        self._started = True

        try:
            await self._async_start()
        except Exception:
            _LOGGER.exception("Failed to start the %s coordinator; it isn't started again until the entry reloads", self.device_type)
            return

        if not self._torn_down:
            await self.async_submit(DesiredState(maximum_setpoint=self.maximum_setpoint))

    async def _async_start(self) -> None:
        """Start the backend's own subscriptions and listeners; by default there are none."""

    async def async_will_remove_from_hass(self) -> None:
        """Tear down for good: release the flame listener and a pending listener notification.

        Backends call this first, then release their own resources. Safe to call more than once.
        """
        self._torn_down = True

        if self._flame_listener_unsub is not None:
            self._flame_listener_unsub()
            self._flame_listener_unsub = None

        if self._listeners_unsub is not None:
            self._listeners_unsub()
            self._listeners_unsub = None

    def snapshot(self) -> BoilerSnapshot:
        """Read the device and derived values once; take it right after the update step of the cycle. The last one is kept for `async_submit`."""
        self._snapshot = BoilerSnapshot(
            boiler=self.boiler,
            flame=self.flame,
            maximum_relative_modulation_value=self.maximum_relative_modulation_value,
            manufacturer=self.manufacturer,
            readings=self.data.readings(),
        )

        return self._snapshot

    @property
    def channel_states(self) -> Mapping[Quantity, ChannelState]:
        """Current tracked state per channel; empty for backends without channels."""
        return MappingProxyType({}) if self._channels is None else self._channels.states

    def confirmed_value(self, quantity: Quantity) -> Any:
        """Return the value last sent on the channel if a verify confirmed it (`IN_SYNC`, also when adopted as sent); None otherwise or without such a channel."""
        state = self.channel_states.get(quantity)
        return state.sent if state is not None and state.status == ChannelStatus.IN_SYNC else None

    @callback
    def verify(self, snapshot: BoilerSnapshot) -> None:
        """Verify the previous cycle's writes against `snapshot`; backends without channels have nothing to verify."""
        if self._channels is not None:
            self._channels.verify(snapshot)
            self._async_send_channels_updated()

    @callback
    def _async_send_channels_updated(self) -> None:
        """Signal this entry's channel synchro sensors; without a config entry there are none."""
        if self.config_entry is not None:
            async_dispatcher_send(self.hass, SIGNAL_CHANNELS_UPDATED.format(self.config_entry.entry_id))

    async def async_control_heating_loop(self, climate: Optional[SatClimate] = None, pwm_state: Optional[PWMState] = None, _time=None) -> None:
        """Control the heating loop for the device."""
        # Update Flame State
        self._flame.update(boiler_state=self.boiler, pwm_state=pwm_state)

        # Update Device State
        if not self.device_active:
            self._device_on_since = None
        elif self._device_on_since is None:
            self._device_on_since = monotonic()

        # See if we can determine the manufacturer (deprecated)
        if self._manufacturer is None and self.member_id is not None:
            manufacturers = ManufacturerFactory.resolve_by_member_id(self.member_id)
            self._manufacturer = manufacturers[0] if len(manufacturers) > 0 else None

        # Nothing further to do without the temperature
        if self.boiler_temperature is None:
            return

        # Handle the temperature tracker
        if self.setpoint is not None and self.boiler_temperature_derivative is not None and self.device_status is not BoilerStatus.HOT_WATER:
            self._boiler_temperature_tracker.update(
                flame_active=self.flame_active,
                setpoint=round(self.setpoint, 0),
                boiler_temperature=round(self.boiler_temperature, 0),
                boiler_temperature_derivative=self.boiler_temperature_derivative
            )

        # Append current boiler temperature if valid and unique
        if self.boiler_temperature is not None:
            current_time = monotonic()
            if not self._boiler_temperatures or self._boiler_temperatures[-1][0] != current_time:
                self._boiler_temperatures.append((current_time, self.boiler_temperature))

        # Remove old temperature records beyond the allowed age
        self._boiler_temperatures = [
            (timestamp, temperature)
            for timestamp, temperature in self._boiler_temperatures
            if seconds_since(timestamp) <= MAX_BOILER_TEMPERATURE_AGE
        ]

        # Update the cold temperature of the boiler
        if boiler_temperature_cold := self._get_latest_boiler_cold_temperature():
            self._boiler_temperature_cold = boiler_temperature_cold
        elif self._boiler_temperature_cold is not None:
            self._boiler_temperature_cold = min(self.boiler_temperature, self._boiler_temperature_cold)

    async def async_submit(self, desired: DesiredState) -> None:
        """Reconcile `desired` through the channels, if the backend has them; fields without a channel are ignored.

        Otherwise apply every requested field through the matching setter, in a fixed order; None and RELEASE fields are skipped. The max
        relative modulation and heater state are skipped when the last snapshot shows them already applied; without a snapshot they are always written.
        """
        if self._channels is not None:
            await self._channels.async_write(desired)
            self._async_send_channels_updated()
            return

        snapshot = self._snapshot

        if desired.control_setpoint is not None and desired.control_setpoint is not RELEASE:
            await self.async_set_control_setpoint(desired.control_setpoint)
        if desired.maximum_relative_modulation is not None and (snapshot is None or snapshot.maximum_relative_modulation_value != desired.maximum_relative_modulation):
            await self.async_set_control_max_relative_modulation(desired.maximum_relative_modulation)
        if desired.heater_state is not None and (snapshot is None or snapshot.boiler.is_active != (desired.heater_state == DeviceState.ON)):
            await self.async_set_heater_state(desired.heater_state)
        if desired.maximum_setpoint is not None:
            await self.async_set_control_max_setpoint(desired.maximum_setpoint)
        if desired.thermostat_setpoint is not None:
            await self.async_set_control_thermostat_setpoint(desired.thermostat_setpoint)

    async def async_set_heater_state(self, state: DeviceState) -> None:
        """Set the state of the device heater."""
        _LOGGER.info("Set central heater state %s", state)

    async def async_set_control_setpoint(self, value: float) -> None:
        """Control the boiler setpoint temperature for the device."""
        if self.supports_setpoint_management:
            _LOGGER.info("Set control boiler setpoint to %d°C", value)

    async def async_set_control_hot_water_setpoint(self, value: float) -> None:
        """Control the DHW setpoint temperature for the device."""
        if self.supports_hot_water_setpoint_management:
            _LOGGER.info("Set control hot water setpoint to %d°C", value)

    async def async_set_control_max_setpoint(self, value: float) -> None:
        """Control the maximum setpoint temperature for the device."""
        if self.supports_maximum_setpoint_management:
            _LOGGER.info("Set maximum setpoint to %d°C", value)

    async def async_set_control_max_relative_modulation(self, value: int) -> None:
        """Control the maximum relative modulation for the device."""
        if self.supports_relative_modulation_management:
            _LOGGER.info("Set maximum relative modulation to %d%%", value)

    async def async_set_control_thermostat_setpoint(self, value: float) -> None:
        """Control the setpoint temperature for the thermostat."""
        pass

    async def async_notify_listeners(self, _time=None) -> None:
        """Notify listeners of an update asynchronously."""
        # Make sure we do not spam
        self._async_unsub_refresh()
        self._debounced_refresh.async_cancel()

        # Inform the listeners that we are updated
        self.async_update_listeners()

    @callback
    def async_set_updated_data(self, data: dict, *, restored: bool = False) -> None:
        """Update the stored data and notify listeners if changes are detected; `restored` marks values loaded from storage.

        A torn-down coordinator ignores the data, e.g. serial data queued before the teardown.
        """
        if self._torn_down:
            return

        # Update the internal data store with new values
        self.data.update(data, restored=restored)

        if self.data.is_dirty():
            # Cancel previous scheduled run, if any
            if self._listeners_unsub is not None:
                self._listeners_unsub()
                self._listeners_unsub = None

            # Confirm that we've taken care of the changes
            self.data.reset_dirty()

            # Notify listeners to ensure the entities are updated
            self._listeners_unsub = async_call_later(self.hass, 5, HassJob(self.async_notify_listeners))

    def _get_latest_boiler_cold_temperature(self) -> Optional[float]:
        """Get the latest boiler cold temperature based on recent boiler temperatures."""
        max_temperature = None

        for timestamp, temperature in self._boiler_temperatures:
            is_before_device_on = self._device_on_since is None or timestamp < self._device_on_since
            is_before_flame_on = self._flame.on_since is None or timestamp < self._flame.on_since

            if is_before_device_on and is_before_flame_on:
                max_temperature = max(max_temperature, temperature) if max_temperature is not None else temperature

        return max_temperature


class SatEntityCoordinator(DataUpdateCoordinator):
    def get(self, domain: str, key: str) -> Optional[Any]:
        """Get the value for the given `key` from the boiler data.

        :param domain: Domain of where this value is located.
        :param key: Key of the value to retrieve from the boiler data.
        :return: Value for the given key from the boiler data, or None if the boiler data or the value are not available.
        """
        entity_id = self._get_entity_id(domain, key)
        if entity_id is None:
            return None

        state = self.hass.states.get(self._get_entity_id(domain, key))
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None

        return state.state

    @abstractmethod
    def _get_entity_id(self, domain: str, key: str):
        pass
