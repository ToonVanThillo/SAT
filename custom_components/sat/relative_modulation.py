import logging
from enum import Enum

from .boiler import BoilerState
from .const import ModulationMode

_LOGGER = logging.getLogger(__name__)


# Enum to represent different states of relative modulation
class RelativeModulationState(str, Enum):
    HEATER_OFF_REQUESTED = "heater_off_requested"
    HOT_WATER = "hot_water"
    MINIMUM = "minimum"
    UNRESTRICTED = "unrestricted"


class RelativeModulation:
    def __init__(self, heating_system: str, limits_hot_water: bool):
        """Initialize instance variables; `limits_hot_water` is the coordinator's `relative_modulation_limits_hot_water`"""
        self._heating_system: str = heating_system
        self._limits_hot_water: bool = limits_hot_water
        self._state: RelativeModulationState = RelativeModulationState.MINIMUM

        _LOGGER.debug("Relative Modulation initialized for heating system: %s", heating_system)

    def update(self, boiler: BoilerState, mode: ModulationMode, heater_off_requested: bool) -> None:
        """Decide the state from the cycle's boiler state, PWM modulation mode and heater request. Where the max relative modulation
        limits hot water: HOT_WATER while hot water is active, then HEATER_OFF_REQUESTED when the cycle requests the heater OFF.
        Otherwise the mode: UNRESTRICTED or MINIMUM."""
        if self._limits_hot_water and boiler.hot_water_active:
            self._state = RelativeModulationState.HOT_WATER
        elif self._limits_hot_water and heater_off_requested:
            self._state = RelativeModulationState.HEATER_OFF_REQUESTED
        elif mode == ModulationMode.UNRESTRICTED:
            self._state = RelativeModulationState.UNRESTRICTED
        else:
            self._state = RelativeModulationState.MINIMUM

    @property
    def state(self) -> RelativeModulationState:
        """The state the last update decided; MINIMUM before the first update"""
        return self._state

    @property
    def enabled(self) -> bool:
        """Whether the relative modulation is unrestricted (the configured maximum), i.e. its state is not MINIMUM"""
        return self._state != RelativeModulationState.MINIMUM
