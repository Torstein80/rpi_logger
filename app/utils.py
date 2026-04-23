from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, available_timezones

DEFAULT_TIMEZONE_LOCATION = "Europe/Oslo"


def parse_timezone_offset(offset_text: str) -> int:
    value = (offset_text or "+00:00").strip()
    match = re.fullmatch(r"([+-])(\d{1,2})(?::?(\d{2}))?", value)
    if not match:
        raise ValueError("Timezone offset must look like +02:00 or -0530.")
    sign, hours_text, minutes_text = match.groups()
    hours = int(hours_text)
    minutes = int(minutes_text or "00")
    if hours > 23 or minutes > 59:
        raise ValueError("Timezone offset is out of range.")
    total = hours * 60 + minutes
    if sign == "-":
        total *= -1
    return total


def format_epoch(epoch: int | None, offset_minutes: int = 0) -> str | None:
    if epoch is None:
        return None
    tz = timezone(timedelta(minutes=offset_minutes))
    return datetime.fromtimestamp(epoch, tz=tz).strftime("%Y-%b-%d %H:%M:%S")


def offset_minutes_to_text(offset_minutes: int) -> str:
    sign = "+" if offset_minutes >= 0 else "-"
    total = abs(offset_minutes)
    hours, minutes = divmod(total, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def timezone_offset_for_location(location: str, epoch: int | None = None) -> int:
    zone_name = (location or DEFAULT_TIMEZONE_LOCATION).strip() or DEFAULT_TIMEZONE_LOCATION
    moment = datetime.fromtimestamp(epoch or int(datetime.now(tz=timezone.utc).timestamp()), tz=timezone.utc)
    tz = ZoneInfo(zone_name)
    offset = moment.astimezone(tz).utcoffset() or timedelta(0)
    return int(offset.total_seconds() // 60)


ALL_TIMEZONE_LOCATIONS = sorted(tz for tz in available_timezones() if not tz.startswith(("Etc/", "Factory")))


def build_timezone_location_options(reference_epoch: int | None = None) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for location in ALL_TIMEZONE_LOCATIONS:
        offset_text = offset_minutes_to_text(timezone_offset_for_location(location, reference_epoch))
        options.append(
            {
                "value": location,
                "label": location.replace("_", " "),
                "offset_text": offset_text,
            }
        )
    return options


def slugify(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", text.strip())
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "session"


def safe_filename(session_name: str, start_epoch: int | None, stop_epoch: int | None, extension: str) -> str:
    start = datetime.utcfromtimestamp(start_epoch).strftime("%Y%m%dT%H%M%SZ") if start_epoch else "unknown-start"
    stop = datetime.utcfromtimestamp(stop_epoch).strftime("%Y%m%dT%H%M%SZ") if stop_epoch else "running"
    return f"{slugify(session_name)}_{start}_{stop}.{extension}"


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)
