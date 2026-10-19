from __future__ import annotations

import logging
import typing
from datetime import datetime

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfPower, UnitOfTemperature, UnitOfVolume
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, Event, EventStateChangedData, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .channels import ChannelStatus, Quantity, diagnostic_status, held_problem_expiry
from .const import CONF_MODE, MODE_SERIAL, CONF_NAME, DOMAIN, COORDINATOR, CLIMATE, MODE_SIMULATOR, CONF_MINIMUM_CONSUMPTION, CONF_MAXIMUM_CONSUMPTION, SIGNAL_CHANNELS_UPDATED
from .coordinator import SatDataUpdateCoordinator
from .entity import SatEntity, SatClimateEntity
from .serial import sensor as serial_sensor
from .simulator import sensor as simulator_sensor

if typing.TYPE_CHECKING:
    pass

_LOGGER: logging.Logger = logging.getLogger(__name__)


async def async_setup_entry(_hass: HomeAssistant, _config_entry: ConfigEntry, _async_add_entities: AddEntitiesCallback):
    """
    Add sensors for the serial protocol if the integration is set to use it.
    """
    # The coordinator is stored before the platforms are set up; the channel sensors don't depend on the climate.
    coordinator: SatDataUpdateCoordinator = _hass.data[DOMAIN][_config_entry.entry_id][COORDINATOR]
    _async_add_entities([SatChannelSynchroSensor(coordinator, _config_entry, quantity) for quantity in coordinator.channel_states])

    # Some sanity checks before we continue
    if CLIMATE not in _hass.data[DOMAIN][_config_entry.entry_id]:
        return

    climate = _hass.data[DOMAIN][_config_entry.entry_id][CLIMATE]

    # Check if integration is set to use the serial protocol
    if _config_entry.data.get(CONF_MODE) == MODE_SERIAL:
        await serial_sensor.async_setup_entry(_hass, _config_entry, _async_add_entities)

    # Check if integration is set to use the simulator
    if _config_entry.data.get(CONF_MODE) == MODE_SIMULATOR:
        await simulator_sensor.async_setup_entry(_hass, _config_entry, _async_add_entities)

    _async_add_entities([
        SatFlameSensor(coordinator, _config_entry),
        SatBoilerSensor(coordinator, _config_entry),
        SatManufacturerSensor(coordinator, _config_entry),
        SatErrorValueSensor(coordinator, _config_entry, climate),
        SatHeatingCurveSensor(coordinator, _config_entry, climate),
    ])

    if coordinator.supports_relative_modulation_management:
        _async_add_entities([SatCurrentPowerSensor(coordinator, _config_entry)])

        if float(_config_entry.options.get(CONF_MINIMUM_CONSUMPTION) or 0) > 0 and float(_config_entry.options.get(CONF_MAXIMUM_CONSUMPTION) or 0) > 0:
            _async_add_entities([SatCurrentConsumptionSensor(coordinator, _config_entry)])


class SatCurrentPowerSensor(SatEntity, SensorEntity):

    @property
    def name(self) -> str:
        return f"Current Power {self._config_entry.data.get(CONF_NAME)} (Boiler)"

    @property
    def device_class(self):
        """Return the device class."""
        return SensorDeviceClass.POWER

    @property
    def native_unit_of_measurement(self):
        """Return the unit of measurement."""
        return UnitOfPower.KILO_WATT

    @property
    def available(self):
        """Return availability of the sensor."""
        return self._coordinator.boiler_power is not None

    @property
    def native_value(self) -> float:
        """Return the state of the device in native units.

        In this case, the state represents the current power of the boiler in kW.
        """
        return self._coordinator.boiler_power

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return f"{self._config_entry.data.get(CONF_NAME).lower()}-boiler-current-power"


class SatCurrentConsumptionSensor(SatEntity, SensorEntity):

    def __init__(self, coordinator: SatDataUpdateCoordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)

        self._minimum_consumption = self._config_entry.options.get(CONF_MINIMUM_CONSUMPTION)
        self._maximum_consumption = self._config_entry.options.get(CONF_MAXIMUM_CONSUMPTION)

    @property
    def name(self) -> str:
        return f"Current Consumption {self._config_entry.data.get(CONF_NAME)} (Boiler)"

    @property
    def device_class(self):
        """Return the device class."""
        return SensorDeviceClass.GAS

    @property
    def native_unit_of_measurement(self):
        """Return the unit of measurement."""
        return UnitOfVolume.CUBIC_METERS

    @property
    def available(self):
        """Return availability of the sensor."""
        return self._coordinator.relative_modulation_value is not None

    @property
    def native_value(self) -> float:
        """Return the state of the device in native units.

        In this case, the state represents the current consumption of the boiler in m³/h.
        """

        if not self._coordinator.device_active:
            return 0

        if not self._coordinator.flame_active:
            return 0

        differential_gas_consumption = self._maximum_consumption - self._minimum_consumption
        relative_modulation_value = self._coordinator.relative_modulation_value

        return round(self._minimum_consumption + ((relative_modulation_value / 100) * differential_gas_consumption), 3)

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return f"{self._config_entry.data.get(CONF_NAME).lower()}-boiler-current-consumption"


class SatHeatingCurveSensor(SatClimateEntity, SensorEntity):

    async def async_added_to_hass(self) -> None:
        async def on_state_change(_event: Event[EventStateChangedData]):
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._climate.entity_id], on_state_change
            )
        )

    @property
    def name(self) -> str:
        return f"Heating Curve {self._config_entry.data.get(CONF_NAME)}"

    @property
    def device_class(self):
        """Return the device class."""
        return SensorDeviceClass.TEMPERATURE

    @property
    def native_unit_of_measurement(self):
        """Return the unit of measurement."""
        return UnitOfTemperature.CELSIUS

    @property
    def available(self):
        """Return availability of the sensor."""
        return self._climate.extra_state_attributes.get("heating_curve") is not None

    @property
    def native_value(self) -> float:
        """Return the state of the device in native units.

        In this case, the state represents the current heating curve value.
        """
        return self._climate.extra_state_attributes.get("heating_curve")

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return f"{self._config_entry.data.get(CONF_NAME).lower()}-heating-curve"


class SatErrorValueSensor(SatClimateEntity, SensorEntity):

    async def async_added_to_hass(self) -> None:
        async def on_state_change(_event: Event[EventStateChangedData]):
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._climate.entity_id], on_state_change
            )
        )

    @property
    def name(self) -> str:
        return f"Error Value {self._config_entry.data.get(CONF_NAME)}"

    @property
    def device_class(self):
        """Return the device class."""
        return SensorDeviceClass.TEMPERATURE

    @property
    def native_unit_of_measurement(self):
        """Return the unit of measurement."""
        return UnitOfTemperature.CELSIUS

    @property
    def available(self):
        """Return availability of the sensor."""
        return self._climate.extra_state_attributes.get("error") is not None

    @property
    def native_value(self) -> float:
        """Return the state of the device in native units.

        In this case, the state represents the current error value.
        """
        return self._climate.extra_state_attributes.get("error")

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return f"{self._config_entry.data.get(CONF_NAME).lower()}-error-value"


class SatManufacturerSensor(SatEntity, SensorEntity):
    @property
    def name(self) -> str:
        return "Boiler Manufacturer"

    @property
    def native_value(self) -> str:
        manufacturer = self._coordinator.manufacturer
        return manufacturer.friendly_name if manufacturer is not None else None

    @property
    def available(self) -> bool:
        return self._coordinator.manufacturer is not None

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.data.get(CONF_NAME).lower()}-manufacturer"


class SatFlameSensor(SatEntity, SensorEntity):
    @property
    def name(self) -> str:
        return "Flame Status"

    @property
    def native_value(self) -> str:
        return self._coordinator.flame.health_status.name

    @property
    def available(self) -> bool:
        return self._coordinator.flame.health_status is not None

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.data.get(CONF_NAME).lower()}-flame-status"


class SatBoilerSensor(SatEntity, SensorEntity):
    @property
    def name(self) -> str:
        return "Boiler Status"

    @property
    def native_value(self) -> str:
        return self._coordinator.device_status.name

    @property
    def available(self) -> bool:
        return self._coordinator.device_status is not None

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.data.get(CONF_NAME).lower()}-boiler-status"


class SatChannelSynchroSensor(SatEntity, SensorEntity):
    """Diagnostic status of one channel, written after each channel verify or write and when a held problem expires."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SatDataUpdateCoordinator, config_entry: ConfigEntry, quantity: Quantity) -> None:
        super().__init__(coordinator, config_entry)

        self._quantity = quantity
        self._unsub_expiry: CALLBACK_TYPE | None = None
        self._attr_options = [status.value for status in ChannelStatus if status != ChannelStatus.PENDING]
        self._attr_name = f"{quantity.value.replace('_', ' ').title()} Synchro"
        self._attr_unique_id = f"{config_entry.data[CONF_NAME].lower()}-channel-{quantity.value}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        signal = SIGNAL_CHANNELS_UPDATED.format(self._config_entry.entry_id)
        self.async_on_remove(async_dispatcher_connect(self.hass, signal, self._handle_coordinator_update))
        self.async_on_remove(self._async_cancel_expiry)

    @property
    def native_value(self) -> str:
        return diagnostic_status(self._coordinator.channel_states[self._quantity], dt_util.utcnow()).value

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write the state, replacing the timer that writes it again when a held problem expires."""
        self._async_cancel_expiry()
        if (expires_at := held_problem_expiry(self._coordinator.channel_states[self._quantity], dt_util.utcnow())) is not None:
            self._unsub_expiry = async_track_point_in_utc_time(self.hass, self._async_expired, expires_at)

        super()._handle_coordinator_update()

    @callback
    def _async_expired(self, _now: datetime) -> None:
        self._unsub_expiry = None
        self._handle_coordinator_update()

    @callback
    def _async_cancel_expiry(self) -> None:
        if self._unsub_expiry is not None:
            self._unsub_expiry()
            self._unsub_expiry = None
