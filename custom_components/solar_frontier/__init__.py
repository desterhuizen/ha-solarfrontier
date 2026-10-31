"""The Solar Frontier integration."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from python_solarfrontier.api import SolarFrontierAPI

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import (
    DayChartCoordinator,
    ErrorsCoordinator,
    LcdCoordinator,
    MeasurementsCoordinator,
    SystemInfoCoordinator,
    YieldCoordinator,
)
from .firmware import probe_capabilities

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Solar Frontier from a config entry."""

    hass.data.setdefault(DOMAIN, {})

    # Create an instance of your API class
    api = SolarFrontierAPI(entry.data[CONF_HOST])

    # Validate the API connection
    if not await api.test_connection():
        await api.close()  # Close the session if connection fails
        return False

    # Models differ in which pages they serve, so only build coordinators for
    # the ones this inverter answers. The sensor platform in turn creates an
    # entity per key those coordinators come back with.
    capabilities = await probe_capabilities(api)

    # The inverter answers one connection at a time; this keeps the
    # coordinators from polling over the top of each other.
    lock = asyncio.Lock()

    coordinators = {
        "system_info": SystemInfoCoordinator(hass, api, timedelta(hours=6), lock)
    }
    if capabilities.day_chart_path:
        coordinators["day_chart"] = DayChartCoordinator(
            hass,
            api,
            timedelta(minutes=5),
            lock,
            capabilities.day_chart_path,
            # The charted power is a last resort: it is a ten-minute average.
            include_power=not capabilities.has_measurements
            and not capabilities.has_lcd,
        )
    if capabilities.extra_yields:
        coordinators["yields"] = YieldCoordinator(
            hass, api, timedelta(minutes=30), lock, capabilities.extra_yields
        )
    if capabilities.has_measurements:
        coordinators["measurements"] = MeasurementsCoordinator(
            hass, api, timedelta(seconds=20), lock
        )
    if capabilities.has_lcd:
        # Each poll walks the display, which takes a few seconds and is visible
        # to anyone standing at the inverter, so do it sparingly.
        coordinators["lcd"] = LcdCoordinator(hass, api, timedelta(minutes=5), lock)
    if capabilities.has_errors:
        coordinators["errors"] = ErrorsCoordinator(
            hass, api, timedelta(minutes=30), lock
        )

    if not capabilities.day_chart_path and not capabilities.has_measurements:
        _LOGGER.warning(
            "Inverter at %s serves neither a yield chart nor a measurements "
            "table; only system information will be available",
            entry.data[CONF_HOST],
        )

    # Fetch initial data so we have data when entities subscribe
    for coordinator in coordinators.values():
        await coordinator.async_refresh()

    # Store the API and coordinator
    hass.data[DOMAIN][entry.entry_id] = {"api": api, **coordinators}

    # Set up your platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
    # Close the API session
        data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if data:
            await data["api"].close()

    return unload_ok
