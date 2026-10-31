"""Reading inverter pages that python-solarfrontier does not cover.

The library targets the SF-WR-5503x web interface. Other models serve a
different dialect of it: the Steca-based SF-WR-3000 names the day chart
``gen.chart.yield.day.js`` rather than ``gen.yield.day.chart.js``, carries the
running total in a ``label_value`` element instead of ``labelValueId``, and
serves no measurements table or month/year/total charts at all. An unknown path
makes that firmware close the connection without replying, which the library
reports as a yield of ``0.0`` -- a number that would read as a meter reset.

``probe_capabilities`` therefore asks an inverter what it actually serves, and
``read_chart`` parses a yield chart in whichever dialect it speaks.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import re

import aiohttp
from python_solarfrontier.api import SolarFrontierAPI
from python_solarfrontier.const import (
    PATH_MEASUREMENTS,
    PATH_YIELD_DAY,
    PATH_YIELD_MONTH,
    PATH_YIELD_TOTAL,
    PATH_YIELD_YEAR,
)
from python_solarfrontier.utils import SolarFrontierWebInfoParser

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

# The library's spelling first, then the reversed one used by the Steca firmware.
DAY_CHART_PATHS = (PATH_YIELD_DAY, "/gen.chart.yield.day.js")

EXTRA_YIELD_PATHS = {
    "month": PATH_YIELD_MONTH,
    "year": PATH_YIELD_YEAR,
    "total": PATH_YIELD_TOTAL,
}

# Both element ids seen holding the running total, e.g. "   0.064kWh 31.10.2026".
TOTAL_RE = re.compile(
    r"getElementById\(\"(?:labelValueId|label_value)\"\)\.innerHTML\s*=\s*"
    r"\"[^\"]*?(\d+(?:\.\d+)?\s*[kM]?Wh)"
)
# The charted samples, and the unit their axis is labelled with.
SERIES_RE = re.compile(r"data\s*:\s*\[(.*?)\]", re.DOTALL)
SCALE_UNIT_RE = re.compile(r"scaleLabel\s*:\s*\"<%=value%>\s*([kM]?W)\"")


@dataclass
class Capabilities:
    """What a particular inverter serves."""

    day_chart_path: str | None = None
    has_measurements: bool = False
    extra_yields: dict[str, str] = field(default_factory=dict)


async def _get(api: SolarFrontierAPI, path: str) -> str | None:
    """Fetch a page, or None if the inverter answers with anything but 200."""
    async with api.session.get(f"{api.host}{path}", timeout=REQUEST_TIMEOUT) as response:
        if response.status != 200:
            return None
        return await response.text()


async def _probe(api: SolarFrontierAPI, path: str) -> str | None:
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
        text = await _probe(api, path)
        if text and (TOTAL_RE.search(text) or SERIES_RE.search(text)):
            capabilities.day_chart_path = path
            break

    text = await _probe(api, PATH_MEASUREMENTS)
    if text and SolarFrontierWebInfoParser.parse_measurements(text):
        capabilities.has_measurements = True

    for name, path in EXTRA_YIELD_PATHS.items():
        text = await _probe(api, path)
        if text and TOTAL_RE.search(text):
            capabilities.extra_yields[name] = path

    _LOGGER.debug(
        "Inverter capabilities: day chart %s, measurements %s, extra yields %s",
        capabilities.day_chart_path,
        capabilities.has_measurements,
        sorted(capabilities.extra_yields),
    )
    return capabilities


def _parse_total(text: str) -> str | None:
    """Return the running total as a "<value><unit>" string, as the library does."""
    match = TOTAL_RE.search(text)
    return match.group(1) if match else None


def _parse_charted_power(text: str) -> str | None:
    """Return the charted power for the slot the inverter is currently in.

    The series is one sample per equal slice of the day. The slot in progress is
    only written once it ends, so fall back to the one before it rather than
    reporting a zero the inverter has simply not filled in yet.
    """
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
    if not samples:
        return None

    now = dt_util.now()
    slot = (now.hour * 60 + now.minute) * len(samples) // (24 * 60)
    slot = min(slot, len(samples) - 1)
    value = next((samples[i] for i in (slot, slot - 1) if i >= 0 and samples[i]), 0.0)
    return f"{value}{unit}"


async def read_chart(api: SolarFrontierAPI, path: str) -> tuple[str | None, str | None]:
    """Return (running total, charted power) from a yield chart page.

    Raises aiohttp.ClientError so a coordinator can mark its entities
    unavailable, rather than reporting a missing reading as zero.
    """
    text = await _get(api, path)
    if text is None:
        return None, None
    return _parse_total(text), _parse_charted_power(text)
