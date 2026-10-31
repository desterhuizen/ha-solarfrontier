"""Reading the inverter's physical display over HTTP.

Firmware that serves no measurements table still shows every live reading on
the front panel, and the web interface mirrors that panel as a 128x64 one-bit
screenshot driven by the same buttons. This module walks that menu and reads
each screen by matching glyph bitmaps against lcd_font: the display uses a
single fixed bitmap face, so exact matching is both simpler and more reliable
than real OCR, and an unfamiliar glyph is reported rather than guessed at.

Only UP is ever pressed. SET, ESC, BOTHMIDDLE and LONGSET can enter the
inverter's configuration menus, and this module never sends them.
"""
from __future__ import annotations

import asyncio
import logging
import re
import struct

import aiohttp

from .lcd_font import FONT

_LOGGER = logging.getLogger(__name__)

PATH_SCREENSHOT = "/gen.screenshot.bmp"
PATH_BUTTON_UP = "/page.main.html?BUTTON_PRESSED=UP"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
# The panel needs a moment to redraw after a press, and the web server answers
# one connection at a time.
SETTLE_SECONDS = 0.4

# Rows of the screen holding the caption and the reading. Below them sit a
# separator and a status bar whose clock changes every minute.
BANDS = {"title": (0, 16), "value": (26, 54)}
SCROLLBAR_X = 122  # the scroll indicator on the right edge is not text
WORD_GAP = 3  # a wider column gap than this separates words, not letters

# "19619kg", "21.7kWh", "-W" when the inverter has nothing to report.
READING_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*(.*)$")


def _decode_bmp(data: bytes) -> list[list[int]]:
    """Return the screenshot as rows of 0/1, where 1 is an ink pixel."""
    if data[:2] != b"BM":
        raise ValueError("not a BMP")
    offbits = struct.unpack_from("<I", data, 10)[0]
    width, height = struct.unpack_from("<ii", data, 18)
    bpp = struct.unpack_from("<H", data, 28)[0]
    if bpp != 1:
        raise ValueError(f"expected a 1bpp screenshot, got {bpp}bpp")

    # Palette entries are BGRA; whichever is darker is the ink.
    dark_first = sum(data[54:57]) < sum(data[58:61])
    ink_bit = 0 if dark_first else 1

    bottom_up = height > 0
    height = abs(height)
    row_bytes = ((width + 31) // 32) * 4

    rows = []
    for y in range(height):
        src = height - 1 - y if bottom_up else y
        start = offbits + src * row_bytes
        rows.append(
            [
                1 if (data[start + (x >> 3)] >> (7 - (x & 7))) & 1 == ink_bit else 0
                for x in range(width)
            ]
        )
    return rows


def _glyph_spans(band: list[list[int]]) -> list[tuple[int, int]]:
    """Split a band into glyph column ranges on blank columns."""
    inked = [any(row[x] for row in band) for x in range(len(band[0]))]
    spans: list[tuple[int, int]] = []
    start = None
    for x, has_ink in enumerate(inked):
        if has_ink and start is None:
            start = x
        elif not has_ink and start is not None:
            spans.append((start, x))
            start = None
    if start is not None:
        spans.append((start, len(inked)))
    return spans


def _glyph_key(band: list[list[int]], x0: int, x1: int) -> tuple[str, ...] | None:
    """Crop a glyph to its ink bounding box and key it the way the font is keyed."""
    sub = [row[x0:x1] for row in band]
    inked = [y for y, row in enumerate(sub) if any(row)]
    if not inked:
        return None
    return tuple(
        "".join(map(str, sub[y])) for y in range(inked[0], inked[-1] + 1)
    )


def _read_band(rows: list[list[int]], band: str) -> tuple[str, int]:
    """Return a band's text, and how many glyphs were not in the font."""
    y0, y1 = BANDS[band]
    cropped = [row[:SCROLLBAR_X] for row in rows[y0:y1]]
    text: list[str] = []
    unknown = 0
    previous_end = None
    for x0, x1 in _glyph_spans(cropped):
        key = _glyph_key(cropped, x0, x1)
        if key is None:
            continue
        if previous_end is not None and x0 - previous_end >= WORD_GAP:
            text.append(" ")
        char = FONT.get(key)
        if char is None:
            char = "?"
            unknown += 1
        text.append(char)
        previous_end = x1
    return "".join(text), unknown


def read_screen(data: bytes) -> tuple[str, str | None, str | None]:
    """Return (caption, reading, unit) for one screenshot.

    The reading is None when the inverter shows a dash, which it does for every
    live value while it is asleep, and when the screen could not be read.
    """
    rows = _decode_bmp(data)
    title, title_unknown = _read_band(rows, "title")
    value, value_unknown = _read_band(rows, "value")
    if title_unknown or value_unknown:
        _LOGGER.warning(
            "Unreadable glyphs on the inverter display (caption %r, reading %r)",
            title,
            value,
        )
        if title_unknown:
            return title, None, None

    # Large digits are spaced widely enough to look like separate words.
    compact = value.replace(" ", "")
    match = READING_RE.match(compact)
    if not match:
        return title, None, compact.lstrip("-") or None
    return title, match.group(1), match.group(2) or None


async def _get(api, path: str) -> bytes:
    async with api.session.get(f"{api.host}{path}", timeout=REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        return await response.read()


async def sweep(api, max_steps: int = 20) -> dict[str, tuple[str | None, str | None]]:
    """Walk the whole menu once, returning {caption: (reading, unit)}.

    Each screen names itself, so captions are read rather than counted and
    someone pressing buttons at the inverter cannot put this out of step. A
    press that does not register leaves the caption unchanged, which is retried
    rather than mistaken for having come back round to the first screen.
    """
    readings: dict[str, tuple[str | None, str | None]] = {}
    first_title: str | None = None
    previous_title: str | None = None

    for _ in range(max_steps):
        title, value, unit = read_screen(await _get(api, PATH_SCREENSHOT))
        if title and title == first_title and previous_title != title and readings:
            break
        if title and title != previous_title:
            readings[title] = (value, unit)
            if first_title is None:
                first_title = title
        previous_title = title
        await _get(api, PATH_BUTTON_UP)
        await asyncio.sleep(SETTLE_SECONDS)

    _LOGGER.debug("Read %d screens from the inverter display", len(readings))
    return readings
