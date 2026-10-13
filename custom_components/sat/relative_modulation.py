import logging
from enum import Enum

from .boiler import BoilerState
from .const import MINIMUM_SETPOINT

_LOGGER = logging.getLogger(__name__)


# Enum to represent different states of relative modulation
class RelativeModulationState(str, Enum):
    OFF = "off"
    COLD = "cold"
    HOT_WATER = "hot_water"
    PULSE_WIDTH_MODULATION_OFF = "pulse_width_modulation_off"


class RelativeModulation:
    def __init__(self, heating_system: str):
        """Initialize instance variables"""
        self._heating_system: str = heating_system
        self._pulse_width_modulation_enabled: bool = False

        _LOGGER.debug("Relative Modulation initialized for heating system: %s", heating_system)

    async def update(self, pulse_width_modulation_enabled: bool) -> None:
        """Update internal state with new internal data"""
        self._pulse_width_modulation_enabled = pulse_width_modulation_enabled

    def state(self, boiler: BoilerState) -> RelativeModulationState:
        """Determine the current state of relative modulation based on the given boiler state and internal data"""
        if boiler.hot_water_active:
            return RelativeModulationState.HOT_WATER

        if boiler.setpoint is None or boiler.setpoint <= MINIMUM_SETPOINT:
            return RelativeModulationState.COLD

        if self._pulse_width_modulation_enabled:
            return RelativeModulationState.OFF

        return RelativeModulationState.PULSE_WIDTH_MODULATION_OFF

    def enabled(self, boiler: BoilerState) -> bool:
        """Check if the relative modulation is enabled based on its current state"""
        return self.state(boiler) != RelativeModulationState.OFF
