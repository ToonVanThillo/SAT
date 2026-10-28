from __future__ import annotations

import logging
import typing
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfPower, UnitOfTemperature, UnitOfTime, UnitOfVolume
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .channels import ChannelStatus, Quantity, diagnostic_status, held_problem_expiry
from .const import CONF_MODE, MODE_SERIAL, CONF_NAME, DOMAIN, COORDINATOR, CLIMATE, MODE_SIMULATOR, CONF_MINIMUM_CONSUMPTION, CONF_MAXIMUM_CONSUMPTION, SIGNAL_CHANNELS_UPDATED, SIGNAL_CLIMATE_UPDATED, SIGNAL_PWM_UPDATED, PWMStatus
from .coordinator import SatDataUpdateCoordinator
from .entity import SatEntity, SatClimateEntity
from .serial import sensor as serial_sensor
from .simulator import sensor as simulator_sensor

if typing.TYPE_CHECKING:
    from .climate import SatClimate

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
        SatReliableFlowTemperatureSensor(coordinator, _config_entry),
        SatManufacturerSensor(coordinator, _config_entry),
        SatErrorValueSensor(coordinator, _config_entry, climate),
        SatHeatingCurveSensor(coordinator, _config_entry, climate),
        SatCalculatedSetpointSensor(coordinator, _config_entry, climate),
    ])
    _async_add_entities([SatControlSensor(coordinator, _config_entry, climate, description) for description in CONTROL_SENSORS])

    if coordinator.supports_relative_modulation_management:
        _async_add_entities([SatCurrentPowerSensor(coordinator, _config_entry)])

        if float(_config_entry.options.get(CONF_MINIMUM_CONSUMPTION) or 0) > 0 and float(_config_entry.options.get(CONF_MAXIMUM_CONSUMPTION) or 0) > 0:
            _async_add_entities([SatCurrentConsumptionSensor(coordinator, _config_entry)])

    if coordinator.supports_setpoint_management:
        _async_add_entities([
            SatPWMStatusSensor(coordinator, _config_entry),
            SatPWMCyclesPerHourSensor(coordinator, _config_entry, climate),
            SatPWMDutyCycleSensor(coordinator, _config_entry, climate),
        ])


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


class SatClimateValueSensor(SatClimateEntity, SensorEntity):
    """A sensor of a value of the climate's control loop, written only after each write of the climate state.

    The climate itself writes on every coordinator update, so the coordinator listener that CoordinatorEntity's async_added_to_hass
    registers is deliberately skipped; it would only double the writes.
    """

    async def async_added_to_hass(self) -> None:
        signal = SIGNAL_CLIMATE_UPDATED.format(self._config_entry.entry_id)
        self.async_on_remove(async_dispatcher_connect(self.hass, signal, self.async_write_ha_state))


class SatHeatingCurveSensor(SatClimateValueSensor):

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
    def available(self) -> bool:
        """Return availability of the sensor."""
        return self._climate.heating_curve.value is not None

    @property
    def native_value(self) -> float | None:
        """Return the current heating curve value."""
        return self._climate.heating_curve.value

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return f"{self._config_entry.data.get(CONF_NAME).lower()}-heating-curve"


class SatCalculatedSetpointSensor(SatClimateValueSensor):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    @property
    def name(self) -> str:
        return f"Calculated Setpoint {self._config_entry.data.get(CONF_NAME)}"

    @property
    def native_value(self) -> float | None:
        return self._climate.calculated_setpoint

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.data.get(CONF_NAME).lower()}-calculated-setpoint"


class SatErrorValueSensor(SatClimateValueSensor):
    """The largest error, in °C; no temperature device class, since it is an offset that HA would convert as an absolute temperature."""

    @property
    def name(self) -> str:
        return f"Error Value {self._config_entry.data.get(CONF_NAME)}"

    @property
    def native_unit_of_measurement(self):
        """Return the unit of measurement."""
        return UnitOfTemperature.CELSIUS

    @property
    def available(self) -> bool:
        """Always available: the error is 0 while the target or inside temperature is unknown."""
        return True

    @property
    def native_value(self) -> float:
        """Return the largest error of the climate and its areas."""
        return self._climate.max_error.value

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


class SatReliableFlowTemperatureSensor(SatEntity, SensorEntity):
    """Diagnostic flow temperature the control logic reads: the current reading if reliable, else the lower of the raw reading and
    the last reliable one, else (before the first reliable reading) the raw one."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: SatDataUpdateCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator, config_entry)

        self._attr_name = "Reliable Flow Temperature"
        self._attr_unique_id = f"{config_entry.data[CONF_NAME].lower()}-reliable-flow-temperature"

    @property
    def native_value(self) -> float | None:
        return self._coordinator.reliable_boiler_temperature


class SatChannelSynchroSensor(SatEntity, SensorEntity):
    """Diagnostic status of one channel, written after each channel verify or write and when a held problem expires."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

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


class SatPWMStatusSensor(SatEntity, SensorEntity):
    """Diagnostic status of the PWM controller; ON during the on-phase, OFF during the off-phase, IDLE when PWM is not active."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_options = [s.value for s in PWMStatus]

    def __init__(self, coordinator: SatDataUpdateCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator, config_entry)

        self._status = PWMStatus.IDLE
        self._attr_name = "PWM Status"
        self._attr_unique_id = f"{config_entry.data[CONF_NAME].lower()}-pwm-status"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        signal = SIGNAL_PWM_UPDATED.format(self._config_entry.entry_id)
        self.async_on_remove(async_dispatcher_connect(self.hass, signal, self._on_pwm_updated))

    @callback
    def _on_pwm_updated(self, status: PWMStatus) -> None:
        self._status = status
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        return self._status.value


class SatPWMCyclesPerHourSensor(SatClimateEntity, SensorEntity):
    """Diagnostic cycles per hour the PWM controller uses; latched for the running cycle, otherwise following the outside temperature."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "cycles/h"

    def __init__(self, coordinator: SatDataUpdateCoordinator, config_entry: ConfigEntry, climate: SatClimate) -> None:
        super().__init__(coordinator, config_entry, climate)

        self._attr_name = "PWM Cycles Per Hour"
        self._attr_unique_id = f"{config_entry.data[CONF_NAME].lower()}-pwm-cycles-per-hour"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        signal = SIGNAL_PWM_UPDATED.format(self._config_entry.entry_id)
        self.async_on_remove(async_dispatcher_connect(self.hass, signal, self._on_pwm_updated))

    @callback
    def _on_pwm_updated(self, _status: PWMStatus) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return self._climate.pwm.cycles_per_hour


class SatPWMDutyCycleSensor(SatClimateEntity, SensorEntity):
    """Diagnostic duty cycle PWM switches with: the on-time as a percentage of the on- and off-time; unknown while PWM has none or both
    times are 0."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: SatDataUpdateCoordinator, config_entry: ConfigEntry, climate: SatClimate) -> None:
        super().__init__(coordinator, config_entry, climate)

        self._attr_name = f"PWM Duty Cycle {config_entry.data.get(CONF_NAME)}"
        self._attr_unique_id = f"{config_entry.data[CONF_NAME].lower()}-pwm-duty-cycle"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        signal = SIGNAL_PWM_UPDATED.format(self._config_entry.entry_id)
        self.async_on_remove(async_dispatcher_connect(self.hass, signal, self._on_pwm_updated))

    @callback
    def _on_pwm_updated(self, _status: PWMStatus) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        if (duty_cycle := self._climate.pwm.duty_cycle) is None:
            return None

        # A cycle time below a second gives no on- or off-time
        on_time, off_time = duty_cycle
        if on_time + off_time == 0:
            return None

        return round(on_time / (on_time + off_time) * 100, 1)


@dataclass(frozen=True, kw_only=True)
class SatControlSensorEntityDescription(SensorEntityDescription):
    """A diagnostic sensor of a control-loop value; the key is the suffix of its unique ID."""

    value_fn: Callable[[SatClimate, SatDataUpdateCoordinator], float | None]


CONTROL_SENSORS: tuple[SatControlSensorEntityDescription, ...] = (
    # The PID terms are offsets added to a setpoint; a temperature device class would convert them as absolute temperatures
    SatControlSensorEntityDescription(
        key="proportional", name="Proportional", native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda climate, _coordinator: climate.pid.proportional,
    ),
    SatControlSensorEntityDescription(
        key="integral", name="Integral", native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda climate, _coordinator: climate.pid.integral,
    ),
    SatControlSensorEntityDescription(
        key="derivative", name="Derivative", native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda climate, _coordinator: climate.pid.derivative,
    ),
    SatControlSensorEntityDescription(key="kp", name="Kp", value_fn=lambda climate, _coordinator: climate.pid.kp),
    SatControlSensorEntityDescription(key="ki", name="Ki", value_fn=lambda climate, _coordinator: climate.pid.ki),
    SatControlSensorEntityDescription(key="kd", name="Kd", value_fn=lambda climate, _coordinator: climate.pid.kd),
    SatControlSensorEntityDescription(
        key="minimum-setpoint", name="Minimum Setpoint", device_class=SensorDeviceClass.TEMPERATURE, native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda climate, _coordinator: climate.minimum_setpoint_value,
    ),
    SatControlSensorEntityDescription(
        key="autotune-coefficient", name="Autotune Coefficient",
        value_fn=lambda climate, _coordinator: climate.heating_curve.optimal_coefficient,
    ),
    SatControlSensorEntityDescription(
        key="flame-average-on-time", name="Flame Average On-Time", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.SECONDS,
        value_fn=lambda _climate, coordinator: coordinator.flame.average_on_time_seconds,
    ),
)


class SatControlSensor(SatClimateValueSensor):
    """Diagnostic sensor of a control-loop value, given by its description; unknown while the value is None."""

    entity_description: SatControlSensorEntityDescription

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: SatDataUpdateCoordinator, config_entry: ConfigEntry, climate: SatClimate, description: SatControlSensorEntityDescription) -> None:
        super().__init__(coordinator, config_entry, climate)

        self.entity_description = description
        self._attr_name = f"{description.name} {config_entry.data.get(CONF_NAME)}"
        self._attr_unique_id = f"{config_entry.data[CONF_NAME].lower()}-{description.key}"

    @property
    def native_value(self) -> float | None:
        return self.entity_description.value_fn(self._climate, self._coordinator)
