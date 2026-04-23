from __future__ import annotations

import glob
import os
import time
from pathlib import Path

from .config import SENSOR_LABELS, W1_BASE_PATH


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


def list_sensors() -> list[dict]:
    base_path = Path(W1_BASE_PATH)
    sensors: list[dict] = []
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


def read_sensor(sensor: dict, retries: int = 3, retry_delay: float = 0.25) -> float:
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


def read_all_sensors() -> list[dict]:
    readings: list[dict] = []
    for sensor in list_sensors():
        readings.append(
            {
                "sensor_id": sensor["sensor_id"],
                "sensor_name": sensor["sensor_name"],
                "temperature_c": read_sensor(sensor),
            }
        )
    return readings
