"""Sensord for Solar Frontier Inverter."""
import logging
import re

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfMass,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Readings arrive as "<value><unit>": "230.5V", "0.080kWh", "19619kg", "-0.5W".
READING_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*(\S+)$")

# The inverter switches magnitude as a value grows (Wh -> kWh -> MWh), and Home
# Assistant only keeps long-term statistics for a sensor whose unit stays put.
# Each sensor therefore declares a fixed native unit, and the unit a reading
# arrives in selects the factor that converts into it.
TO_NATIVE_UNIT = {
    UnitOfPower.WATT: {"W": 1, "kW": 1000, "MW": 1000000},
    UnitOfEnergy.KILO_WATT_HOUR: {"Wh": 0.001, "kWh": 1, "MWh": 1000},
    UnitOfElectricPotential.VOLT: {"V": 1},
    UnitOfElectricCurrent.AMPERE: {"A": 1},
    UnitOfFrequency.HERTZ: {"Hz": 1},
    UnitOfTemperature.CELSIUS: {"°C": 1, "C": 1},
    UnitOfMass.KILOGRAMS: {"kg": 1},
    UnitOfTime.HOURS: {"h": 1},
}

MEASUREMENT_KINDS = {
    'power': (SensorDeviceClass.POWER, UnitOfPower.WATT),
    'voltage': (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
    'current': (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    'frequency': (SensorDeviceClass.FREQUENCY, UnitOfFrequency.HERTZ),
}

MEASUREMENT_KEYS = [
        'dc_power', 'dc_voltage', 'dc_current', 'ac_voltage', 'ac_current',
        'ac_voltage_phase_1', 'ac_voltage_phase_2',
        'ac_voltage_phase_3', 'ac_current_phase_1', 'ac_current_phase_2', 'ac_current_phase_3',
        'ac_frequency', 'ac_frequency_phase_1', 'ac_frequency_phase_2', 'ac_frequency_phase_3',
        'ac_power', 'ac_power_phase_1', 'ac_power_phase_2', 'ac_power_phase_3'
]


def _yield_sensor(key: str) -> SensorEntityDescription:
    """Build the description for a yield total."""
    return SensorEntityDescription(
        key=key,
        name=f"SF {key.title()} Yield",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )


def _measurement_sensor(key: str) -> SensorEntityDescription:
    """Build the description for a measurement, keyed off what it measures."""
    kind = next(kind for kind in MEASUREMENT_KINDS if kind in key)
    device_class, native_unit = MEASUREMENT_KINDS[kind]
    friendly_name = key.replace('_', ' ').title()
    return SensorEntityDescription(
        key=key,
        name=f"SF {friendly_name}",
        device_class=device_class,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=native_unit,
    )


def _text_sensor(key: str, name: str) -> SensorEntityDescription:
    """Build the description for a reading that is text rather than a number."""
    return SensorEntityDescription(
        key=key,
        name=name,
        entity_category=EntityCategory.DIAGNOSTIC,
    )


SENSOR_DESCRIPTIONS = {
    **{key: _yield_sensor(key) for key in ('day', 'month', 'year', 'total')},
    **{key: _measurement_sensor(key) for key in MEASUREMENT_KEYS},
    # Nameplate rating rather than a reading, so it carries no state class.
    'nominal_power': SensorEntityDescription(
        key='nominal_power',
        name='SF Nominal Power',
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    'internal_temperature': SensorEntityDescription(
        key='internal_temperature',
        name='SF Internal Temperature',
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    'operating_hours': SensorEntityDescription(
        key='operating_hours',
        name='SF Operating Hours',
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS,
    ),
    'co2_saving': SensorEntityDescription(
        key='co2_saving',
        name='SF CO2 Saving',
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
    ),
    'abs_max_power': SensorEntityDescription(
        key='abs_max_power',
        name='SF Max Power All Time',
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    'day_max_power': SensorEntityDescription(
        key='day_max_power',
        name='SF Max Power Today',
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    # A record of the best day ever, not a running total: no state class, so it
    # builds no statistics and cannot be picked as an energy source by mistake.
    'day_max_yield': SensorEntityDescription(
        key='day_max_yield',
        name='SF Best Day Yield',
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    'day_max_power_time': SensorEntityDescription(
        key='day_max_power_time',
        name='SF Max Power Today At',
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    'last_error': _text_sensor('last_error', 'SF Last Error'),
    'model_name': _text_sensor('model_name', 'SF Model'),
    'serial': _text_sensor('serial', 'SF Serial Number'),
    'firmware': _text_sensor('firmware', 'SF Firmware'),
}

# Ordered so a reading from a dedicated page wins over the same reading taken
# off the display, which in turn wins over one derived from the day chart.
COORDINATOR_KEYS = ('measurements', 'lcd', 'day_chart', 'yields', 'system_info', 'errors')


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Solar Frontier sensors based on a config entry."""
    coordinators = hass.data[DOMAIN][entry.entry_id]

    sensors = []
    claimed = set()
    for coordinator_key in COORDINATOR_KEYS:
        coordinator = coordinators.get(coordinator_key)
        if coordinator is None or not coordinator.data:
            continue
        for key in coordinator.data:
            if key.endswith("_attributes"):
                continue
            description = SENSOR_DESCRIPTIONS.get(key)
            if description is None:
                _LOGGER.debug("Inverter reported unknown reading %s", key)
                continue
            if key in claimed:
                continue
            claimed.add(key)
            sensors.append(SolarFrontierSensor(coordinator, description))

    async_add_entities(sensors)

class SolarFrontierSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Solar Frontier Sensor."""

    def __init__(self, coordinator, description: SensorEntityDescription) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"solar_frontier_{description.key}_sensor"

    @property
    def _reading(self):
        """The raw reading for this sensor, or None if there is not one."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self.entity_description.key)

    @property
    def native_value(self):
        """Return the reading, converted into this sensor's native unit."""
        raw_state = self._reading
        if raw_state is None:
            return None

        description = self.entity_description
        if description.device_class is SensorDeviceClass.TIMESTAMP:
            return raw_state
        if description.native_unit_of_measurement is None:
            return str(raw_state)[:255]

        # The library hands back None for a value it could not parse and 0.0
        # when the request itself failed; reporting either as a number would
        # read as a meter reset on the yield sensors.
        if not isinstance(raw_state, str):
            return None
        match = READING_RE.match(raw_state.strip())
        if match is None:
            return None
        factor = TO_NATIVE_UNIT[description.native_unit_of_measurement].get(
            match.group(2)
        )
        if factor is None:
            _LOGGER.warning(
                "Ignoring %s: inverter reported unexpected unit %s",
                description.key,
                match.group(2),
            )
            return None
        return float(match.group(1)) * factor

    @property
    def extra_state_attributes(self):
        """Return the detail a reading carries alongside its value, if any."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(f"{self.entity_description.key}_attributes")
