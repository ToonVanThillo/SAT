from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.template import DOMAIN as TEMPLATE_DOMAIN

from custom_components.sat.const import (
    CONF_AUTOMATIC_DUTY_CYCLE,
    CONF_AUTOMATIC_GAINS,
    CONF_DEVICE,
    CONF_HEATING_SYSTEM,
    CONF_INSIDE_SENSOR_ENTITY_ID,
    CONF_MINIMUM_SETPOINT,
    CONF_MODE,
    CONF_MQTT_TOPIC,
    CONF_NAME,
    CONF_OUTSIDE_SENSOR_ENTITY_ID,
    CONF_OVERSHOOT_PROTECTION,
    HEATING_SYSTEM_RADIATORS,
    MODE_FAKE,
    MODE_MQTT_EMS,
)

DEFAULT_USER_DATA = {
    CONF_MODE: MODE_FAKE,
    CONF_NAME: "Test",
    CONF_DEVICE: None,
    CONF_AUTOMATIC_GAINS: True,
    CONF_AUTOMATIC_DUTY_CYCLE: True,
    CONF_OVERSHOOT_PROTECTION: True,
    CONF_INSIDE_SENSOR_ENTITY_ID: "sensor.test_inside_sensor",
    CONF_OUTSIDE_SENSOR_ENTITY_ID: "sensor.test_outside_sensor",
}


def ems_entry(options: dict | None = None) -> tuple[str, list[tuple]]:
    """Return the parametrization of an entry using the EMS MQTT coordinator, with the given entry options.

    The minimum setpoint is low so that PWM stays disabled unless the options force it.
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
