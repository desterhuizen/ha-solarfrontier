"""Reading inverter pages that python-solarfrontier does not cover.

The library targets the SF-WR-5503x web interface. Other models serve a
different dialect of it: the Steca-based SF-WR-3000 names the day chart
``gen.chart.yield.day.js`` rather than ``gen.yield.day.chart.js``, carries the
running total in a ``label_value`` element instead of ``labelValueId``, and
serves no measurements table or month/year/total charts at all. An unknown path
makes that firmware close the connection without replying, which the library
reports as a yield of ``0.0`` -- a number that would read as a meter reset.

``probe_capabilities`` therefore asks an inverter what it actually serves, and
the readers below parse whichever pages it has. Live readings missing from the
web interface are taken off the physical display instead; see lcd.py.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import logging
import re

import aiohttp
from python_solarfrontier.api import SolarFrontierAPI
from python_solarfrontier.const import (
    PATH_MEASUREMENTS,
    PATH_SYSTEM_INFO,
    PATH_YIELD_DAY,
    PATH_YIELD_MONTH,
    PATH_YIELD_TOTAL,
    PATH_YIELD_YEAR,
)
from python_solarfrontier.utils import SolarFrontierWebInfoParser

from homeassistant.util import dt as dt_util

from .lcd import PATH_SCREENSHOT, read_screen

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

PATH_INFO_SW = "/gen.info.table.sw.js"
PATH_ERRORS = "/gen.errors.table.js"

# The library's spelling first, then the reversed one used by the Steca firmware.
DAY_CHART_PATHS = (PATH_YIELD_DAY, "/gen.chart.yield.day.js")

EXTRA_YIELD_PATHS = {
    "month": PATH_YIELD_MONTH,
    "year": PATH_YIELD_YEAR,
    "total": PATH_YIELD_TOTAL,
}

# Both element ids seen holding the running total, e.g. "   0.080kWh 31.10.2026".
TOTAL_RE = re.compile(
    r"getElementById\(\"(?:labelValueId|label_value)\"\)\.innerHTML\s*=\s*"
    r"\"[^\"]*?(\d+(?:\.\d+)?\s*[kM]?Wh)"
)
# The charted samples, and the unit their axis is labelled with.
SERIES_RE = re.compile(r"data\s*:\s*\[(.*?)\]", re.DOTALL)
SCALE_UNIT_RE = re.compile(r"scaleLabel\s*:\s*\"<%=value%>\s*([kM]?W)\"")
# Rows of any of the inverter's generated tables.
TABLE_ROW_RE = re.compile(r"<tr><td>(.*?)</td><td[^>]*>(.*?)</td>")
ERROR_ROW_RE = re.compile(
    r"<tr><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td>"
)


@dataclass
class Capabilities:
    """What a particular inverter serves."""

    day_chart_path: str | None = None
    has_measurements: bool = False
    has_lcd: bool = False
    has_errors: bool = False
    extra_yields: dict[str, str] = field(default_factory=dict)


async def _get(api: SolarFrontierAPI, path: str) -> bytes | None:
    """Fetch a page, or None if the inverter answers with anything but 200."""
    async with api.session.get(f"{api.host}{path}", timeout=REQUEST_TIMEOUT) as response:
        if response.status != 200:
            return None
        return await response.read()


async def _get_text(api: SolarFrontierAPI, path: str) -> str | None:
    body = await _get(api, path)
    return None if body is None else body.decode("utf-8", "replace")


async def _probe(api: SolarFrontierAPI, path: str) -> bytes | None:
    """Fetch a page while probing, treating any refusal as "not served"."""
    try:
        return await _get(api, path)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


async def probe_capabilities(api: SolarFrontierAPI) -> Capabilities:
    """Ask the inverter which of the pages we know about it actually serves.

    Requests are made one at a time: this web server answers a single
    connection and drops anything asked of it in parallel.
    """
    capabilities = Capabilities()

    for path in DAY_CHART_PATHS:
        body = await _probe(api, path)
        text = None if body is None else body.decode("utf-8", "replace")
        if text and (TOTAL_RE.search(text) or SERIES_RE.search(text)):
            capabilities.day_chart_path = path
            break

    body = await _probe(api, PATH_MEASUREMENTS)
    if body and SolarFrontierWebInfoParser.parse_measurements(
        body.decode("utf-8", "replace")
    ):
        capabilities.has_measurements = True

    for name, path in EXTRA_YIELD_PATHS.items():
        body = await _probe(api, path)
        text = None if body is None else body.decode("utf-8", "replace")
        if text and TOTAL_RE.search(text):
            capabilities.extra_yields[name] = path

    body = await _probe(api, PATH_ERRORS)
    capabilities.has_errors = bool(body and ERROR_ROW_RE.search(
        body.decode("utf-8", "replace")
    ))

    # The display is the only source of live readings on firmware without a
    # measurements table, so only bother with it when there is no table.
    if not capabilities.has_measurements:
        body = await _probe(api, PATH_SCREENSHOT)
        if body:
            try:
                title, _, _ = read_screen(body)
            except ValueError:
                title = ""
            capabilities.has_lcd = bool(title)

    _LOGGER.debug(
        "Inverter capabilities: day chart %s, measurements %s, lcd %s, "
        "errors %s, extra yields %s",
        capabilities.day_chart_path,
        capabilities.has_measurements,
        capabilities.has_lcd,
        capabilities.has_errors,
        sorted(capabilities.extra_yields),
    )
    return capabilities


def _parse_total(text: str) -> str | None:
    """Return the running total as a "<value><unit>" string, as the library does."""
    match = TOTAL_RE.search(text)
    return match.group(1) if match else None


def _parse_samples(text: str) -> tuple[list[float], str] | None:
    """Return the charted power samples and the unit of their axis."""
    series = SERIES_RE.search(text)
    if not series:
        return None
    unit_match = SCALE_UNIT_RE.search(text)
    unit = unit_match.group(1) if unit_match else "W"
    samples = []
    for raw in series.group(1).split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            samples.append(float(raw))
        except ValueError:
            return None
    return (samples, unit) if samples else None


def _slot_of_now(count: int) -> int:
    now = dt_util.now()
    return min((now.hour * 60 + now.minute) * count // (24 * 60), count - 1)


def _charted_power(samples: list[float], unit: str) -> str:
    """The sample for the slot the inverter is currently in.

    The slot in progress is only written once it ends, so fall back to the one
    before it rather than reporting a zero not yet filled in.
    """
    slot = _slot_of_now(len(samples))
    value = next((samples[i] for i in (slot, slot - 1) if i >= 0 and samples[i]), 0.0)
    return f"{value}{unit}"


def _peak_time(samples: list[float]) -> datetime | None:
    """When today's highest charted sample was recorded."""
    peak = max(samples)
    if not peak:
        return None
    minutes = samples.index(peak) * (24 * 60) // len(samples)
    return dt_util.now().replace(
        hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0
    )


async def read_chart(api: SolarFrontierAPI, path: str) -> dict:
    """Read a yield chart page.

    Raises aiohttp.ClientError so a coordinator can mark its entities
    unavailable, rather than reporting a missing reading as zero.
    """
    text = await _get_text(api, path)
    if text is None:
        return {}
    parsed = _parse_samples(text)
    data: dict = {"total": _parse_total(text)}
    if parsed:
        samples, unit = parsed
        data["power"] = _charted_power(samples, unit)
        data["peak_time"] = _peak_time(samples)
    return data


def _table(text: str) -> dict[str, str]:
    return {k.strip(): v.strip() for k, v in TABLE_ROW_RE.findall(text)}


async def read_info(api: SolarFrontierAPI) -> dict:
    """Model, serial, nominal power and the firmware versions on board."""
    data: dict = {}
    text = await _get_text(api, PATH_SYSTEM_INFO)
    if text:
        rows = _table(text)
        data["model_name"] = rows.get("Name")
        data["serial"] = rows.get("Serial")
        data["nominal_power"] = rows.get("Nominal Power")

    text = await _get_text(api, PATH_INFO_SW)
    if text:
        versions = _table(text)
        # The application firmware is the version worth surfacing; the rest are
        # bootloaders and parameter sets.
        data["firmware"] = versions.get("HMI APP")

    return {k: v for k, v in data.items() if v}


async def read_errors(api: SolarFrontierAPI) -> dict:
    """The most recent entry from the inverter's error log.

    Rows are newest first. "Active" means the fault has no end time yet.
    """
    text = await _get_text(api, PATH_ERRORS)
    if text is None:
        return {}
    rows = ERROR_ROW_RE.findall(text)
    rows = [r for r in rows if r[0].strip() and r[0].strip() != "Name"]
    if not rows:
        return {"last_error": "none"}

    name, kind, state, start, end = (c.strip() for c in rows[0])
    return {
        "last_error": name,
        "last_error_attributes": {
            "type": kind,
            "state": state,
            "started": start,
            "ended": end or None,
            "logged_entries": len(rows),
        },
    }
