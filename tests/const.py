import json
from pathlib import Path
from typing import Any

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.template import DOMAIN as TEMPLATE_DOMAIN

from custom_components.sat.const import (
    CONF_AUTOMATIC_GAINS,
    CONF_DEVICE,
    CONF_HEATING_SYSTEM,
    CONF_INSIDE_SENSOR_ENTITY_ID,
    CONF_MINIMUM_SETPOINT,
    CONF_MODE,
    CONF_MQTT_TOPIC,
    CONF_NAME,
    CONF_OUTSIDE_SENSOR_ENTITY_ID,
    HEATING_SYSTEM_RADIATORS,
    MODE_FAKE,
    MODE_MQTT_EMS,
    MODE_MQTT_OPENTHERM,
)

# Storage key of the EMS coordinator on device `ems`
STORE_KEY = "sat_ems_mqtt_coordinator_ems"

DEFAULT_USER_DATA = {
    CONF_MODE: MODE_FAKE,
    CONF_NAME: "Test",
    CONF_DEVICE: None,
    CONF_AUTOMATIC_GAINS: True,
    CONF_INSIDE_SENSOR_ENTITY_ID: "sensor.test_inside_sensor",
    CONF_OUTSIDE_SENSOR_ENTITY_ID: "sensor.test_outside_sensor",
}

# A real EMS-ESP `boiler_data` capture of an idle combi boiler
EMS_BOILER_DATA: dict[str, Any] = json.loads((Path(__file__).parent / "fixtures" / "ems_boiler_data.json").read_text())

# The capture with the values the tests build on: a warm boiler, a return temperature (the capture has none) and a low maximum modulation
IDLE: dict[str, Any] = EMS_BOILER_DATA | {"selflowtemp": 40, "curflowtemp": 40.5, "rettemp": 35, "burnmaxpower": 31}


def ems_entry(options: dict | None = None) -> tuple[str, list[tuple]]:
    """Return the parametrization of an entry using the EMS MQTT coordinator, with the given entry options.

    The minimum setpoint of 20 °C is at or below the cold threshold, so the setup logs an error for it, and with the default inputs the
    calculated setpoint is above it, so PWM runs UNRESTRICTED unless the options disallow unrestricted modulation.
    """
    return (
        "domains, data, options, config",
        [(
            [(TEMPLATE_DOMAIN, 1)],
            {
                CONF_MODE: MODE_MQTT_EMS,
                CONF_DEVICE: "ems",
                CONF_MQTT_TOPIC: "ems-esp",
                CONF_HEATING_SYSTEM: HEATING_SYSTEM_RADIATORS,
                CONF_MINIMUM_SETPOINT: 20,
            },
            options or {},
            {
                TEMPLATE_DOMAIN: [
                    {
                        SENSOR_DOMAIN: [
                            {"name": "test_inside_sensor", "state": "{{ 20.9 | float }}"},
                            {"name": "test_outside_sensor", "state": "{{ 9.9 | float }}"},
                        ]
                    },
                ],
            },
        )],
    )


def with_data(data: dict, options: dict | None = None) -> tuple[str, list[tuple]]:
    """Return the EMS entry parametrization with extra config entry data."""
    names, [(domains, entry_data, entry_options, config)] = ems_entry(options)

    return names, [(domains, entry_data | data, entry_options, config)]


def otgw_entry(options: dict | None = None, data: dict | None = None) -> tuple[str, list[tuple]]:
    """Return the `ems_entry` parametrization switched to the OpenTherm MQTT backend (device `gw1`, topic `otgw`), with extra config entry data."""
    names, [(domains, entry_data, entry_options, config)] = ems_entry(options)
    backend = {CONF_MODE: MODE_MQTT_OPENTHERM, CONF_DEVICE: "gw1", CONF_MQTT_TOPIC: "otgw"}

    return names, [(domains, entry_data | backend | (data or {}), entry_options, config)]
