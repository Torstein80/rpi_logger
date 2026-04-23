from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .config import DEFAULT_FALLBACK_TEMPERATURE_C, SENSOR_LABELS, W1_BASE_PATH


SUPPORTED_PREFIXES = ("10-", "22-", "28-", "3b-", "42-")


def _parse_sensor_labels(mapping_text: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in (mapping_text or "").split(","):
        if ":" not in item:
            continue
        sensor_id, label = item.split(":", 1)
        sensor_id = sensor_id.strip()
        label = label.strip()
        if sensor_id and label:
            mapping[sensor_id] = label
    return mapping


SENSOR_LABEL_MAP = _parse_sensor_labels(SENSOR_LABELS)


def _sensor_base_path() -> Path:
    return Path(W1_BASE_PATH)


def list_sensors() -> list[dict[str, Any]]:
    base_path = _sensor_base_path()
    sensors: list[dict[str, Any]] = []
    if not base_path.exists():
        return sensors

    for entry in sorted(base_path.iterdir()):
        name = entry.name.lower()
        if entry.is_dir() and name.startswith(SUPPORTED_PREFIXES):
            sensors.append(
                {
                    "sensor_id": entry.name,
                    "sensor_name": SENSOR_LABEL_MAP.get(entry.name, entry.name),
                    "device_path": str(entry / "w1_slave"),
                }
            )
    return sensors


def sensor_lookup() -> dict[str, dict[str, Any]]:
    return {sensor["sensor_id"]: sensor for sensor in list_sensors()}


def read_sensor(sensor: dict[str, Any], retries: int = 3, retry_delay: float = 0.25) -> float:
    device_path = sensor["device_path"]
    last_error: Exception | None = None

    for _ in range(retries):
        try:
            with open(device_path, "r", encoding="utf-8") as handle:
                lines = [line.strip() for line in handle.readlines()]
            if len(lines) < 2:
                raise ValueError(f"Incomplete sensor response for {sensor['sensor_id']}.")
            if not lines[0].endswith("YES"):
                raise ValueError(f"CRC check failed for {sensor['sensor_id']}.")
            marker = "t="
            idx = lines[1].find(marker)
            if idx == -1:
                raise ValueError(f"Temperature token not found for {sensor['sensor_id']}.")
            milli_c = int(lines[1][idx + len(marker):])
            return milli_c / 1000.0
        except Exception as exc:  # pragma: no cover - hardware dependent
            last_error = exc
            time.sleep(retry_delay)

    raise RuntimeError(f"Could not read {sensor['sensor_id']}: {last_error}")


def read_detected_sensor(sensor: dict[str, Any], fallback_value_c: float = DEFAULT_FALLBACK_TEMPERATURE_C) -> dict[str, Any]:
    try:
        temperature_c = read_sensor(sensor)
        return {
            "sensor_id": sensor["sensor_id"],
            "sensor_name": sensor["sensor_name"],
            "temperature_c": temperature_c,
            "status": "ok",
            "is_substituted": False,
            "error_text": None,
        }
    except Exception as exc:  # pragma: no cover - hardware dependent
        return {
            "sensor_id": sensor["sensor_id"],
            "sensor_name": sensor["sensor_name"],
            "temperature_c": fallback_value_c,
            "status": "read_error",
            "is_substituted": True,
            "error_text": str(exc),
        }


def read_all_sensors(fallback_value_c: float = DEFAULT_FALLBACK_TEMPERATURE_C) -> list[dict[str, Any]]:
    return [read_detected_sensor(sensor, fallback_value_c) for sensor in list_sensors()]


def read_configured_sensor(
    sensor_id: str,
    sensor_name: str,
    fallback_value_c: float = DEFAULT_FALLBACK_TEMPERATURE_C,
    detected: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    detected_map = detected if detected is not None else sensor_lookup()
    sensor = detected_map.get(sensor_id)
    if sensor is None:
        return {
            "sensor_id": sensor_id,
            "sensor_name": sensor_name,
            "temperature_c": fallback_value_c,
            "status": "missing",
            "is_substituted": True,
            "error_text": f"Configured sensor {sensor_id} is not present on the 1-Wire bus.",
        }

    if "device_path" in sensor:
        reading = read_detected_sensor(sensor, fallback_value_c)
    else:
        reading = {
            "sensor_id": sensor.get("sensor_id", sensor_id),
            "sensor_name": sensor_name,
            "temperature_c": sensor.get("temperature_c", fallback_value_c),
            "status": sensor.get("status", "ok"),
            "is_substituted": bool(sensor.get("is_substituted", False)),
            "error_text": sensor.get("error_text"),
        }
    reading["sensor_name"] = sensor_name
    return reading
