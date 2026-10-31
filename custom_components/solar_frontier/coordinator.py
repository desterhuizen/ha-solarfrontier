"""Coordinators for data fetching."""
import asyncio
from datetime import timedelta
import logging

import aiohttp
from python_solarfrontier.api import SolarFrontierAPI

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .firmware import read_chart

_LOGGER = logging.getLogger(__name__)

class SolarFrontierBaseCoordinator(DataUpdateCoordinator):
    """Base coordinator for fetching data from Solar Frontier inverter."""

    def __init__(self, hass: HomeAssistant, api: SolarFrontierAPI, update_interval: timedelta) -> None:
        """Initialize the base coordinator."""
        self.api = api
        super().__init__(
            hass,
            _LOGGER,
            name="solar_frontier",
            update_interval=update_interval,
        )

    async def _async_update_data(self):
        """Fetch data from Solar Frontier inverter."""
        raise NotImplementedError("Must implement _async_update_data in subclasses")


class SystemInfoCoordinator(SolarFrontierBaseCoordinator):
    """Coordinator for fetching system information from Solar Frontier inverter."""

    async def _async_update_data(self):
        """Fetch system information from Solar Frontier inverter."""
        try:
            return await self.api.get_system_info()
        except aiohttp.ClientError as e:
            raise UpdateFailed(f"Error fetching system info: {e}") from e


class DayChartCoordinator(SolarFrontierBaseCoordinator):
    """Coordinator for the day yield chart, which also carries charted power.

    Read more often than the other yields because it doubles as the power
    reading on firmware that serves no measurements table.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: SolarFrontierAPI,
        update_interval: timedelta,
        path: str,
        include_power: bool,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(hass, api, update_interval)
        self.path = path
        self.include_power = include_power

    async def _async_update_data(self):
        """Fetch the day yield, and the charted power where it is needed."""
        try:
            total, power = await read_chart(self.api, self.path)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise UpdateFailed(f"Error fetching day yield: {e}") from e
        data = {"day": total}
        if self.include_power:
            data["ac_power"] = power
        return data


class YieldCoordinator(SolarFrontierBaseCoordinator):
    """Coordinator for the month, year and total yields the inverter serves."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: SolarFrontierAPI,
        update_interval: timedelta,
        paths: dict[str, str],
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(hass, api, update_interval)
        self.paths = paths

    async def _async_update_data(self):
        """Fetch each supported yield total, one request at a time."""
        data = {}
        for name, path in self.paths.items():
            try:
                data[name], _ = await read_chart(self.api, path)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise UpdateFailed(f"Error fetching {name} yield: {e}") from e
        return data

class MeasurementsCoordinator(SolarFrontierBaseCoordinator):
    """Coordinator for fetching measurement data from Solar Frontier inverter."""

    async def _async_update_data(self):
        """Fetch measurement data from Solar Frontier inverter."""
        try:
            return await self.api.get_measurements()
        except aiohttp.ClientError as e:
            raise UpdateFailed(f"Error fetching measurements: {e}") from e
