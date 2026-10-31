"""Coordinators for data fetching."""
import asyncio
from datetime import timedelta
import logging

import aiohttp
from python_solarfrontier.api import SolarFrontierAPI

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .firmware import read_chart, read_errors, read_info
from .lcd import sweep

_LOGGER = logging.getLogger(__name__)

# Captions as the display spells them, including the firmware's own misspelling
# of "frequency", mapped onto the reading names the sensors use.
LCD_CAPTIONS = {
    "Output power": "ac_power",
    "PV voltage": "dc_voltage",
    "PV current": "dc_current",
    "Grid voltage": "ac_voltage",
    "Grid current": "ac_current",
    "Grid freqency": "ac_frequency",
    "Grid frequency": "ac_frequency",
    "Internal temp.": "internal_temperature",
    "Operating hours": "operating_hours",
    "CO₂ saving": "co2_saving",
    "Abs. max. power": "abs_max_power",
    "Day max. power": "day_max_power",
    "Day max. yield": "day_max_yield",
}


class SolarFrontierBaseCoordinator(DataUpdateCoordinator):
    """Base coordinator for fetching data from Solar Frontier inverter."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: SolarFrontierAPI,
        update_interval: timedelta,
        lock: asyncio.Lock,
    ) -> None:
        """Initialize the base coordinator."""
        self.api = api
        self._lock = lock
        super().__init__(
            hass,
            _LOGGER,
            name="solar_frontier",
            update_interval=update_interval,
        )

    async def _async_update_data(self):
        """Fetch data, one coordinator at a time.

        The inverter's web server answers a single connection, so overlapping
        polls from the different coordinators would fail each other.
        """
        async with self._lock:
            try:
                return await self._fetch()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise UpdateFailed(f"Error talking to the inverter: {e}") from e

    async def _fetch(self):
        """Fetch this coordinator's data."""
        raise NotImplementedError("Must implement _fetch in subclasses")


class SystemInfoCoordinator(SolarFrontierBaseCoordinator):
    """Model, serial, nominal power and firmware version."""

    async def _fetch(self):
        """Fetch system information from Solar Frontier inverter."""
        return await read_info(self.api)


class DayChartCoordinator(SolarFrontierBaseCoordinator):
    """The day yield chart, which also carries a power curve.

    Read more often than the other yields because it doubles as the power
    reading on firmware that serves neither a measurements table nor a display.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: SolarFrontierAPI,
        update_interval: timedelta,
        lock: asyncio.Lock,
        path: str,
        include_power: bool,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(hass, api, update_interval, lock)
        self.path = path
        self.include_power = include_power

    async def _fetch(self):
        """Fetch the day yield, and the charted power where it is needed."""
        chart = await read_chart(self.api, self.path)
        data = {
            "day": chart.get("total"),
            "day_max_power_time": chart.get("peak_time"),
        }
        if self.include_power:
            data["ac_power"] = chart.get("power")
        return data


class YieldCoordinator(SolarFrontierBaseCoordinator):
    """The month, year and total yields, on inverters that chart them."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: SolarFrontierAPI,
        update_interval: timedelta,
        lock: asyncio.Lock,
        paths: dict[str, str],
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(hass, api, update_interval, lock)
        self.paths = paths

    async def _fetch(self):
        """Fetch each supported yield total, one request at a time."""
        return {
            name: (await read_chart(self.api, path)).get("total")
            for name, path in self.paths.items()
        }


class MeasurementsCoordinator(SolarFrontierBaseCoordinator):
    """Coordinator for fetching measurement data from Solar Frontier inverter."""

    async def _fetch(self):
        """Fetch measurement data from Solar Frontier inverter."""
        return await self.api.get_measurements()


class LcdCoordinator(SolarFrontierBaseCoordinator):
    """Live readings taken off the inverter's physical display.

    Walking the display takes a few seconds and leaves it on whichever screen
    the walk ended on, which is visible to anyone standing at the inverter.
    """

    async def _fetch(self):
        """Walk the display and return the readings it showed."""
        data = {}
        for caption, (value, unit) in (await sweep(self.api)).items():
            key = LCD_CAPTIONS.get(caption)
            if key is None:
                _LOGGER.debug("Ignoring unknown display screen %r", caption)
                continue
            # Re-join into the "<value><unit>" form the sensors parse.
            data[key] = None if value is None or unit is None else f"{value}{unit}"
        return data


class ErrorsCoordinator(SolarFrontierBaseCoordinator):
    """The inverter's error log."""

    async def _fetch(self):
        """Fetch the most recent entry from the error log."""
        return await read_errors(self.api)
