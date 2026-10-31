"""Sensord for Solar Frontier Inverter."""
import logging

from python_solarfrontier.utils import UnitConverter

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONVERTER = UnitConverter()

# The inverter switches magnitude as a value grows (Wh -> kWh -> MWh), so the
# reported unit cannot be used as the entity's unit: Home Assistant only keeps
# long-term statistics for a sensor whose unit stays put. Each sensor declares a
# fixed native unit instead, and the reported unit selects the factor that
# converts into it.
TO_NATIVE_UNIT = {
    UnitOfPower.WATT: {"W": 1, "kW": 1000, "MW": 1000000},
    UnitOfEnergy.KILO_WATT_HOUR: {"Wh": 0.001, "kWh": 1, "MWh": 1000},
    UnitOfElectricPotential.VOLT: {"V": 1},
    UnitOfElectricCurrent.AMPERE: {"A": 1},
    UnitOfFrequency.HERTZ: {"Hz": 1},
}

MEASUREMENT_KINDS = {
    'power': (SensorDeviceClass.POWER, UnitOfPower.WATT),
    'voltage': (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
    'current': (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    'frequency': (SensorDeviceClass.FREQUENCY, UnitOfFrequency.HERTZ),
}

MEASUREMENT_KEYS = [
        'dc_power', 'dc_voltage', 'dc_current', 'ac_voltage_phase_1', 'ac_voltage_phase_2',
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


SENSOR_DESCRIPTIONS = {
    **{key: _yield_sensor(key) for key in ('day', 'month', 'year', 'total')},
    # Nameplate rating rather than a reading, so it carries no state class.
    'nominal_power': SensorEntityDescription(
        key='nominal_power',
        name='SF Nominal Power',
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    **{key: _measurement_sensor(key) for key in MEASUREMENT_KEYS},
}

# Ordered so that a reading from a dedicated page wins over the same reading
# derived from the day chart.
COORDINATOR_KEYS = ('measurements', 'day_chart', 'yields', 'system_info')


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
    def native_value(self) -> float | None:
        """Return the reading converted into this sensor's native unit."""
        if not self.coordinator.data:
            return None
        raw_state = self.coordinator.data.get(self.entity_description.key)
        # The library hands back None for a value it could not parse and 0.0
        # when the request itself failed; reporting either as a number would
        # read as a meter reset on the yield sensors.
        if not isinstance(raw_state, str):
            return None
        try:
            value = CONVERTER.get_value(raw_state)
            unit = CONVERTER.get_unit(raw_state)
        except ValueError:
            return None
        factor = TO_NATIVE_UNIT[self.entity_description.native_unit_of_measurement].get(unit)
        if factor is None:
            _LOGGER.warning(
                "Ignoring %s: inverter reported unexpected unit %s",
                self.entity_description.key,
                unit,
            )
            return None
        return value * factor
