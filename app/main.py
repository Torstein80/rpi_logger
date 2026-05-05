from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .config import (
    ALLOWED_EXPORTS,
    APP_HOST,
    APP_PORT,
    DB_PATH,
    DEFAULT_FALLBACK_TEMPERATURE_C,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_POLL_SECONDS,
    DEFAULT_TIMEZONE_OFFSET,
    HTTPS_CERTFILE,
    HTTPS_KEYFILE,
    MAX_SENSOR_SLOTS,
    STORAGE_HEARTBEAT_PATH,
    STORAGE_LOW_FREE_GB_WARNING,
    STORAGE_PROBE_INTERVAL_SECONDS,
    STORAGE_WATCHDOG_ENABLED,
    STORAGE_WATCHDOG_REBOOT_AFTER_SECONDS,
)
from .database import db_session, init_db
from .exporters import build_export
from .models import SESSION_STATUS_COMPLETED, SESSION_STATUS_RUNNING, SESSION_STATUS_SCHEDULED
from .networking import NetworkConfigError, NetworkUnavailableError, apply_wired_network, network_manager_status
from .sensors import list_sensors, read_all_sensors, read_configured_sensor
from .utils import (
    DEFAULT_TIMEZONE_LOCATION,
    build_timezone_location_options,
    format_epoch,
    offset_minutes_to_text,
    parse_timezone_offset,
)


INITIAL_DB_INIT_ERROR: str | None = None
try:
    init_db()
except Exception as exc:  # pragma: no cover - startup environment dependent
    INITIAL_DB_INIT_ERROR = str(exc)


@dataclass
class LoggerState:
    active_session_id: int | None = None
    last_cycle_epoch: int | None = None
    last_error: str | None = None
    last_db_write_epoch: int | None = None
    storage_last_probe_epoch: int | None = None
    storage_last_ok_epoch: int | None = None
    storage_last_error: str | None = None
    storage_unwritable_since_epoch: int | None = None
    heartbeat_last_epoch: int | None = None
    watchdog_reboot_requested_epoch: int | None = None
    watchdog_last_error: str | None = None
    watchdog_reboot_attempts: int = 0
    storage_probe: dict[str, Any] | None = None


LOGGER_STATE = LoggerState()
LOGGER_LOCK = threading.Lock()
PROCESS_START_EPOCH = int(time.time())
RECOVERY_SAMPLE_SESSION_IDS: set[int] = set()


def now_epoch() -> int:
    return int(time.time())


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _directory_write_test(path: Path, current_epoch: int) -> tuple[bool, str | None]:
    probe_path = path / ".write-test.tmp"
    try:
        with open(probe_path, "w", encoding="utf-8") as handle:
            handle.write(str(current_epoch))
            handle.flush()
            os.fsync(handle.fileno())
        probe_path.unlink(missing_ok=True)
        return True, None
    except Exception as exc:  # pragma: no cover - filesystem dependent
        return False, str(exc)


def request_host_reboot() -> tuple[bool, str]:
    dbus_socket = Path("/run/dbus/system_bus_socket")
    env = os.environ.copy()
    if dbus_socket.exists():
        env.setdefault("DBUS_SYSTEM_BUS_ADDRESS", f"unix:path={dbus_socket}")

    commands: list[list[str]] = []
    if shutil.which("dbus-send") and dbus_socket.exists():
        commands.append([
            "dbus-send",
            "--system",
            "--print-reply",
            "--dest=org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager.Reboot",
            "boolean:true",
        ])
    if shutil.which("systemctl"):
        commands.append(["systemctl", "reboot"])
    if shutil.which("reboot"):
        commands.append(["reboot"])

    errors: list[str] = []
    for command in commands:
        try:
            process = subprocess.run(command, capture_output=True, text=True, env=env, timeout=15, check=False)
            if process.returncode == 0:
                return True, "Reboot command accepted."
            errors.append((process.stderr or process.stdout or f"Command {' '.join(command)} failed").strip())
        except Exception as exc:  # pragma: no cover - host integration dependent
            errors.append(str(exc))
    return False, "; ".join(error for error in errors if error) or "No reboot command was available."


def probe_storage_health(current_epoch: int | None = None, write_heartbeat: bool = True) -> dict[str, Any]:
    current_epoch = current_epoch or now_epoch()
    db_path = Path(DB_PATH)
    heartbeat_path = Path(STORAGE_HEARTBEAT_PATH)
    disk = build_disk_stats()
    directory_writable, directory_write_error = _directory_write_test(db_path.parent, current_epoch)

    db_open_ok = False
    db_open_error = None
    try:
        probe_conn = sqlite3.connect(DB_PATH, timeout=1)
        try:
            probe_conn.execute("PRAGMA schema_version;").fetchone()
            db_open_ok = True
        finally:
            probe_conn.close()
    except Exception as exc:  # pragma: no cover - filesystem dependent
        db_open_error = str(exc)

    heartbeat_ok = False
    heartbeat_error = None
    if write_heartbeat:
        try:
            payload = {
                "epoch": current_epoch,
                "host": socket.gethostname(),
                "db_path": DB_PATH,
                "db_open_ok": db_open_ok,
                "directory_writable": directory_writable,
                "scheduler_error": LOGGER_STATE.last_error,
                "last_db_write_epoch": LOGGER_STATE.last_db_write_epoch,
            }
            _atomic_write_text(heartbeat_path, json.dumps(payload, indent=2, sort_keys=True))
            heartbeat_ok = True
        except Exception as exc:  # pragma: no cover - filesystem dependent
            heartbeat_error = str(exc)
    else:
        heartbeat_ok = heartbeat_path.exists()
        if not heartbeat_ok:
            heartbeat_error = "Heartbeat file has not been written yet."

    ok = directory_writable and db_open_ok and (heartbeat_ok or not write_heartbeat)
    summary_level = "ok"
    summary = "Storage path is healthy."
    if not ok:
        summary_level = "bad"
        summary = db_open_error or directory_write_error or heartbeat_error or "Storage path is unavailable."
    elif (disk.get("free_gb") or 0) < STORAGE_LOW_FREE_GB_WARNING:
        summary_level = "warn"
        summary = f"Free space is below {STORAGE_LOW_FREE_GB_WARNING} GiB."

    return {
        **disk,
        "heartbeat_path": str(heartbeat_path),
        "directory_writable": directory_writable,
        "directory_write_error": directory_write_error,
        "db_open_ok": db_open_ok,
        "db_open_error": db_open_error,
        "heartbeat_ok": heartbeat_ok,
        "heartbeat_error": heartbeat_error,
        "probe_epoch": current_epoch,
        "summary_level": summary_level,
        "summary": summary,
        "ok": ok and summary_level != "bad",
        "watchdog_enabled": STORAGE_WATCHDOG_ENABLED,
        "watchdog_reboot_after_seconds": STORAGE_WATCHDOG_REBOOT_AFTER_SECONDS,
        "low_free_gb_warning": STORAGE_LOW_FREE_GB_WARNING,
    }


def update_storage_probe_state(probe: dict[str, Any]) -> None:
    LOGGER_STATE.storage_last_probe_epoch = probe.get("probe_epoch")
    LOGGER_STATE.storage_probe = probe
    if probe.get("db_open_ok") and probe.get("directory_writable") and probe.get("heartbeat_ok"):
        LOGGER_STATE.storage_last_ok_epoch = probe.get("probe_epoch")
        LOGGER_STATE.storage_last_error = None
        LOGGER_STATE.storage_unwritable_since_epoch = None
        LOGGER_STATE.heartbeat_last_epoch = probe.get("probe_epoch")
        LOGGER_STATE.watchdog_reboot_requested_epoch = None
        LOGGER_STATE.watchdog_last_error = None
    else:
        LOGGER_STATE.storage_last_error = probe.get("summary") or probe.get("db_open_error") or probe.get("directory_write_error")
        if LOGGER_STATE.storage_unwritable_since_epoch is None:
            LOGGER_STATE.storage_unwritable_since_epoch = probe.get("probe_epoch")
        if probe.get("heartbeat_ok"):
            LOGGER_STATE.heartbeat_last_epoch = probe.get("probe_epoch")


def maybe_trigger_storage_watchdog(probe: dict[str, Any]) -> None:
    if not STORAGE_WATCHDOG_ENABLED:
        return
    current_epoch = int(probe.get("probe_epoch") or now_epoch())
    if probe.get("db_open_ok") and probe.get("directory_writable") and probe.get("heartbeat_ok"):
        return
    if LOGGER_STATE.storage_unwritable_since_epoch is None:
        LOGGER_STATE.storage_unwritable_since_epoch = current_epoch
    unwritable_for = current_epoch - LOGGER_STATE.storage_unwritable_since_epoch
    if unwritable_for < STORAGE_WATCHDOG_REBOOT_AFTER_SECONDS:
        return
    if LOGGER_STATE.watchdog_reboot_requested_epoch is not None:
        return
    LOGGER_STATE.watchdog_reboot_attempts += 1
    LOGGER_STATE.watchdog_reboot_requested_epoch = current_epoch
    os.sync()
    ok, message = request_host_reboot()
    if ok:
        LOGGER_STATE.watchdog_last_error = None
    else:
        LOGGER_STATE.watchdog_last_error = message


def build_storage_status(current_epoch: int | None = None, write_heartbeat: bool = False) -> dict[str, Any]:
    probe = probe_storage_health(current_epoch=current_epoch, write_heartbeat=write_heartbeat)
    cached = LOGGER_STATE.storage_probe or {}
    return {
        **cached,
        **probe,
        "last_storage_ok_epoch": LOGGER_STATE.storage_last_ok_epoch,
        "last_storage_error": LOGGER_STATE.storage_last_error,
        "last_db_write_epoch": LOGGER_STATE.last_db_write_epoch,
        "heartbeat_last_epoch": LOGGER_STATE.heartbeat_last_epoch,
        "storage_unwritable_since_epoch": LOGGER_STATE.storage_unwritable_since_epoch,
        "unwritable_for_seconds": (current_epoch or probe.get("probe_epoch") or now_epoch()) - LOGGER_STATE.storage_unwritable_since_epoch if LOGGER_STATE.storage_unwritable_since_epoch else 0,
        "watchdog_reboot_requested_epoch": LOGGER_STATE.watchdog_reboot_requested_epoch,
        "watchdog_last_error": LOGGER_STATE.watchdog_last_error,
        "watchdog_reboot_attempts": LOGGER_STATE.watchdog_reboot_attempts,
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(target=scheduler_loop, daemon=True, name="logger-scheduler").start()
    yield


app = FastAPI(title="hagasolutions RPi temp logger", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class StartSessionPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    start_epoch: int | None = Field(default=None)
    interval_seconds: int = Field(ge=1, le=3600)
    timezone_offset: str = Field(default=DEFAULT_TIMEZONE_OFFSET)


class SensorSlotPayload(BaseModel):
    slot_no: int = Field(ge=1, le=MAX_SENSOR_SLOTS)
    enabled: bool = False
    alias: str = Field(default="", max_length=120)
    sensor_id: str | None = Field(default=None, max_length=64)
    fallback_value_c: float = Field(default=DEFAULT_FALLBACK_TEMPERATURE_C)


class SensorSlotsUpdatePayload(BaseModel):
    slots: list[SensorSlotPayload]


class WiredNetworkPayload(BaseModel):
    mode: str = Field(pattern="^(auto|manual)$")
    ip_cidr: str | None = Field(default=None, max_length=64)
    gateway: str | None = Field(default=None, max_length=64)
    dns: str | None = Field(default=None, max_length=256)


def serialize_session(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    start_epoch = row["actual_start_epoch"] or row["scheduled_start_epoch"]
    return {
        "id": row["id"],
        "name": row["name"],
        "status": row["status"],
        "scheduled_start_epoch": row["scheduled_start_epoch"],
        "actual_start_epoch": row["actual_start_epoch"],
        "stop_epoch": row["stop_epoch"],
        "interval_seconds": row["interval_seconds"],
        "timezone_offset_minutes": row["timezone_offset_minutes"],
        "timezone_offset": offset_minutes_to_text(row["timezone_offset_minutes"]),
        "scheduled_start_local": format_epoch(row["scheduled_start_epoch"], row["timezone_offset_minutes"]),
        "actual_start_local": format_epoch(row["actual_start_epoch"], row["timezone_offset_minutes"]),
        "stop_local": format_epoch(row["stop_epoch"], row["timezone_offset_minutes"]),
        "start_epoch": start_epoch,
    }


def fetch_active_or_scheduled(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM logging_sessions
        WHERE status IN (?, ?)
        ORDER BY id ASC
        LIMIT 1
        """,
        (SESSION_STATUS_SCHEDULED, SESSION_STATUS_RUNNING),
    ).fetchone()


def fetch_sessions(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM logging_sessions
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def fetch_session(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM logging_sessions WHERE id = ?", (session_id,)).fetchone()


def fetch_sensor_slots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM sensor_slots ORDER BY slot_no ASC").fetchall()


def fetch_enabled_sensor_slots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sensor_slots WHERE enabled = 1 AND sensor_id IS NOT NULL AND TRIM(sensor_id) <> '' ORDER BY slot_no ASC"
    ).fetchall()


def default_slot_alias(slot_row: sqlite3.Row | dict[str, Any]) -> str:
    alias = (slot_row["alias"] or "").strip()
    if alias:
        return alias
    sensor_id = slot_row["sensor_id"]
    if sensor_id:
        return sensor_id
    return f"Slot {slot_row['slot_no']}"


def fetch_latest_readings_for_session(conn: sqlite3.Connection, session_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT r.slot_no, r.sensor_id, r.sensor_name, r.temperature_c, r.sample_epoch, r.status, r.is_substituted, r.error_text
        FROM temperature_readings r
        JOIN (
            SELECT slot_no, MAX(sample_epoch) AS max_epoch
            FROM temperature_readings
            WHERE session_id = ?
            GROUP BY slot_no
        ) latest
          ON latest.slot_no = r.slot_no
         AND latest.max_epoch = r.sample_epoch
        WHERE r.session_id = ?
        ORDER BY r.slot_no ASC
        """,
        (session_id, session_id),
    ).fetchall()
    return [
        {
            "slot_no": row["slot_no"],
            "sensor_id": row["sensor_id"],
            "sensor_name": row["sensor_name"],
            "temperature_c": round(row["temperature_c"], 3),
            "sample_epoch": row["sample_epoch"],
            "status": row["status"],
            "is_substituted": bool(row["is_substituted"]),
            "error_text": row["error_text"],
        }
        for row in rows
    ]


def fetch_readings(conn: sqlite3.Connection, session_id: int, limit: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT sample_epoch, slot_no, sensor_id, sensor_name, temperature_c, status, is_substituted, error_text
        FROM temperature_readings
        WHERE session_id = ?
        ORDER BY sample_epoch DESC, slot_no ASC, sensor_id ASC
        LIMIT ?
        """,
        (session_id, limit),
    ).fetchall()
    return list(reversed(rows))


def _reading_row_to_dict(row: sqlite3.Row, timezone_offset_minutes: int) -> dict[str, Any]:
    return {
        "sample_epoch": row["sample_epoch"],
        "sample_time_local": format_epoch(row["sample_epoch"], timezone_offset_minutes),
        "slot_no": row["slot_no"],
        "sensor_id": row["sensor_id"],
        "sensor_name": row["sensor_name"],
        "temperature_c": round(row["temperature_c"], 4) if row["temperature_c"] is not None else None,
        "status": row["status"],
        "is_substituted": bool(row["is_substituted"]),
        "error_text": row["error_text"],
    }


def augment_reading_items_with_gaps(
    items: list[dict[str, Any]],
    interval_seconds: int,
    timezone_offset_minutes: int,
) -> list[dict[str, Any]]:
    if not items or interval_seconds <= 0:
        return items

    grouped: dict[int | str, list[dict[str, Any]]] = {}
    for item in items:
        key = item.get("slot_no") or item.get("sensor_id") or "unknown"
        grouped.setdefault(key, []).append(dict(item))

    gap_threshold = max(int(interval_seconds * 1.5), interval_seconds + 1)
    merged: list[dict[str, Any]] = []
    for group_items in grouped.values():
        group_items.sort(key=lambda row: (int(row["sample_epoch"]), int(row.get("slot_no") or 0)))
        previous: dict[str, Any] | None = None
        for current in group_items:
            if previous is not None:
                delta = int(current["sample_epoch"]) - int(previous["sample_epoch"])
                if delta > gap_threshold:
                    gap_epoch = int(previous["sample_epoch"]) + interval_seconds
                    offline_seconds = max(int(current["sample_epoch"]) - gap_epoch, 0)
                    resume_local = format_epoch(int(current["sample_epoch"]), timezone_offset_minutes)
                    merged.append(
                        {
                            "sample_epoch": gap_epoch,
                            "sample_time_local": format_epoch(gap_epoch, timezone_offset_minutes),
                            "slot_no": current.get("slot_no") or previous.get("slot_no"),
                            "sensor_id": current.get("sensor_id") or previous.get("sensor_id"),
                            "sensor_name": current.get("sensor_name") or previous.get("sensor_name"),
                            "temperature_c": None,
                            "status": "offline_gap",
                            "is_substituted": True,
                            "error_text": f"Logger offline or unavailable for {offline_seconds} s. Logging resumed at {resume_local}.",
                            "gap_resume_epoch": int(current["sample_epoch"]),
                            "gap_duration_seconds": offline_seconds,
                        }
                    )
            merged.append(current)
            previous = current

    merged.sort(
        key=lambda row: (
            int(row["sample_epoch"]),
            int(row.get("slot_no") or 0),
            0 if row.get("status") == "offline_gap" else 1,
        )
    )
    return merged


def fetch_reading_items(conn: sqlite3.Connection, session_row: sqlite3.Row, limit: int) -> list[dict[str, Any]]:
    rows = fetch_readings(conn, session_row["id"], limit)
    items = [_reading_row_to_dict(row, session_row["timezone_offset_minutes"]) for row in rows]
    return augment_reading_items_with_gaps(items, session_row["interval_seconds"], session_row["timezone_offset_minutes"])


def fetch_chart_seed_rows(conn: sqlite3.Connection, session_id: int, limit: int = 200) -> list[dict[str, Any]]:
    session_row = fetch_session(conn, session_id)
    if session_row is None:
        return []
    return fetch_reading_items(conn, session_row, limit)


def validate_sensor_slots_payload(payload: SensorSlotsUpdatePayload) -> None:
    if not payload.slots:
        raise HTTPException(status_code=400, detail="At least one slot definition must be provided.")

    slot_numbers = [item.slot_no for item in payload.slots]
    if len(set(slot_numbers)) != len(slot_numbers):
        raise HTTPException(status_code=400, detail="Slot numbers must be unique.")

    active_sensor_ids: list[str] = []
    for item in payload.slots:
        sensor_id = (item.sensor_id or "").strip() or None
        if item.enabled and not sensor_id:
            raise HTTPException(status_code=400, detail=f"Slot {item.slot_no} is enabled but has no sensor ID.")
        if sensor_id and item.enabled:
            active_sensor_ids.append(sensor_id)

    if len(set(active_sensor_ids)) != len(active_sensor_ids):
        raise HTTPException(status_code=400, detail="The same sensor ID cannot be assigned to multiple enabled slots.")


def save_sensor_slots(conn: sqlite3.Connection, payload: SensorSlotsUpdatePayload) -> list[sqlite3.Row]:
    validate_sensor_slots_payload(payload)
    current_epoch = now_epoch()
    for item in payload.slots:
        sensor_id = (item.sensor_id or "").strip() or None
        alias = item.alias.strip()
        conn.execute(
            """
            UPDATE sensor_slots
            SET enabled = ?, alias = ?, sensor_id = ?, fallback_value_c = ?, updated_epoch = ?
            WHERE slot_no = ?
            """,
            (
                1 if item.enabled else 0,
                alias,
                sensor_id,
                float(item.fallback_value_c),
                current_epoch,
                item.slot_no,
            ),
        )
    return fetch_sensor_slots(conn)


def build_runtime_sensor_slots(
    slot_rows: list[sqlite3.Row],
    detected_sensors: list[dict[str, Any]],
    latest_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    detected_map = {item["sensor_id"]: item for item in detected_sensors}
    latest_by_slot = {item["slot_no"]: item for item in (latest_rows or [])}
    items: list[dict[str, Any]] = []

    for row in slot_rows:
        sensor_id = row["sensor_id"]
        live = detected_map.get(sensor_id) if sensor_id else None
        latest = latest_by_slot.get(row["slot_no"])
        if row["enabled"] and sensor_id:
            if live is None:
                runtime_status = "missing"
                current_temp = row["fallback_value_c"]
                current_error = f"Configured sensor {sensor_id} not detected."
                is_online = False
                is_substituted = True
            else:
                runtime_status = live["status"] if live["status"] != "ok" else "online"
                current_temp = live["temperature_c"]
                current_error = live["error_text"]
                is_online = live["status"] == "ok"
                is_substituted = bool(live["is_substituted"])
        elif row["enabled"]:
            runtime_status = "unassigned"
            current_temp = None
            current_error = "No sensor configured for this enabled slot."
            is_online = False
            is_substituted = False
        else:
            runtime_status = "disabled"
            current_temp = None
            current_error = None
            is_online = False
            is_substituted = False

        items.append(
            {
                "slot_no": row["slot_no"],
                "enabled": bool(row["enabled"]),
                "alias": row["alias"],
                "display_name": default_slot_alias(row),
                "sensor_id": sensor_id,
                "fallback_value_c": row["fallback_value_c"],
                "runtime_status": runtime_status,
                "current_temperature_c": current_temp,
                "current_error": current_error,
                "is_online": is_online,
                "is_substituted": is_substituted,
                "last_logged_temperature_c": latest["temperature_c"] if latest else None,
                "last_logged_status": latest["status"] if latest else None,
                "last_logged_epoch": latest["sample_epoch"] if latest else None,
                "last_logged_is_substituted": latest["is_substituted"] if latest else None,
                "last_logged_error_text": latest["error_text"] if latest else None,
            }
        )
    return items


def build_disk_stats() -> dict[str, Any]:
    db_path = Path(DB_PATH)
    try:
        usage = shutil.disk_usage(str(db_path.parent))
    except Exception as exc:  # pragma: no cover - filesystem dependent
        return {
            "db_path": DB_PATH,
            "db_exists": db_path.exists(),
            "db_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
            "db_size_mb": round((db_path.stat().st_size if db_path.exists() else 0) / (1024 ** 2), 3),
            "free_bytes": None,
            "total_bytes": None,
            "used_bytes": None,
            "free_gb": None,
            "used_gb": None,
            "total_gb": None,
            "used_percent": None,
            "disk_error": str(exc),
        }
    gib = 1024 ** 3
    mib = 1024 ** 2
    db_size_bytes = db_path.stat().st_size if db_path.exists() else 0
    return {
        "db_path": DB_PATH,
        "db_exists": db_path.exists(),
        "db_size_bytes": db_size_bytes,
        "db_size_mb": round(db_size_bytes / mib, 3),
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_gb": round(usage.free / gib, 3),
        "used_gb": round(usage.used / gib, 3),
        "total_gb": round(usage.total / gib, 3),
        "used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0.0,
        "disk_error": None,
    }


def create_session_record(conn: sqlite3.Connection, payload: StartSessionPayload) -> sqlite3.Row:
    offset_minutes = parse_timezone_offset(payload.timezone_offset)
    current_epoch = now_epoch()
    requested_start_epoch = current_epoch if payload.start_epoch is None else int(payload.start_epoch)
    if requested_start_epoch < 0:
        raise HTTPException(status_code=400, detail="start_epoch must be zero or greater.")

    existing = fetch_active_or_scheduled(conn)
    if existing:
        raise HTTPException(status_code=409, detail="A session is already scheduled or running.")

    enabled_slots = fetch_enabled_sensor_slots(conn)
    if not enabled_slots:
        raise HTTPException(status_code=400, detail="Configure at least one enabled sensor slot before starting logging.")

    status = SESSION_STATUS_SCHEDULED if requested_start_epoch > current_epoch else SESSION_STATUS_RUNNING
    actual_start_epoch = None if status == SESSION_STATUS_SCHEDULED else current_epoch
    next_sample_epoch = requested_start_epoch if status == SESSION_STATUS_SCHEDULED else current_epoch

    cursor = conn.execute(
        """
        INSERT INTO logging_sessions (
            name, scheduled_start_epoch, actual_start_epoch, stop_epoch, interval_seconds,
            timezone_offset_minutes, status, created_epoch, next_sample_epoch
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)
        """,
        (
            payload.name.strip(),
            requested_start_epoch,
            actual_start_epoch,
            payload.interval_seconds,
            offset_minutes,
            status,
            current_epoch,
            next_sample_epoch,
        ),
    )
    session_row = fetch_session(conn, cursor.lastrowid)
    if status == SESSION_STATUS_RUNNING:
        sample_session(conn, session_row, current_epoch)
        session_row = fetch_session(conn, cursor.lastrowid)
    return session_row


def mark_session_running(conn: sqlite3.Connection, session_id: int, actual_start_epoch: int) -> sqlite3.Row:
    conn.execute(
        """
        UPDATE logging_sessions
        SET status = ?, actual_start_epoch = ?, next_sample_epoch = ?
        WHERE id = ?
        """,
        (SESSION_STATUS_RUNNING, actual_start_epoch, actual_start_epoch, session_id),
    )
    return fetch_session(conn, session_id)


def stop_session_record(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row:
    session_row = fetch_session(conn, session_id)
    if session_row is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session_row["status"] not in {SESSION_STATUS_RUNNING, SESSION_STATUS_SCHEDULED}:
        raise HTTPException(status_code=409, detail="Session is already stopped.")
    actual_start = session_row["actual_start_epoch"] or session_row["scheduled_start_epoch"]
    conn.execute(
        """
        UPDATE logging_sessions
        SET status = ?, actual_start_epoch = ?, stop_epoch = ?
        WHERE id = ?
        """,
        (SESSION_STATUS_COMPLETED, actual_start, now_epoch(), session_id),
    )
    return fetch_session(conn, session_id)


def delete_session_record(conn: sqlite3.Connection, session_id: int) -> None:
    session_row = fetch_session(conn, session_id)
    if session_row is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session_row["status"] in {SESSION_STATUS_RUNNING, SESSION_STATUS_SCHEDULED}:
        raise HTTPException(status_code=409, detail="Stop the active or scheduled session before deleting it.")
    conn.execute("DELETE FROM logging_sessions WHERE id = ?", (session_id,))


def sample_session(conn: sqlite3.Connection, session_row: sqlite3.Row, sample_epoch: int) -> None:
    slots = fetch_enabled_sensor_slots(conn)
    if not slots:
        raise RuntimeError("No configured sensor slots are enabled.")

    detected_sensor_defs = {item["sensor_id"]: item for item in list_sensors()}

    for slot in slots:
        sensor_name = default_slot_alias(slot)
        reading = read_configured_sensor(
            sensor_id=slot["sensor_id"],
            sensor_name=sensor_name,
            fallback_value_c=slot["fallback_value_c"],
            detected=detected_sensor_defs,
        )
        conn.execute(
            """
            INSERT INTO temperature_readings (
                session_id, sample_epoch, slot_no, sensor_id, sensor_name, temperature_c, status, is_substituted, error_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_row["id"],
                sample_epoch,
                slot["slot_no"],
                reading["sensor_id"],
                reading["sensor_name"],
                reading["temperature_c"],
                reading["status"],
                1 if reading["is_substituted"] else 0,
                reading["error_text"],
            ),
        )
    conn.execute(
        "UPDATE logging_sessions SET next_sample_epoch = ? WHERE id = ?",
        (sample_epoch + session_row["interval_seconds"], session_row["id"]),
    )


def scheduler_loop() -> None:
    while True:
        try:
            with LOGGER_LOCK:
                with db_session() as conn:
                    target = fetch_active_or_scheduled(conn)
                    if target is None:
                        LOGGER_STATE.active_session_id = None
                        LOGGER_STATE.last_cycle_epoch = now_epoch()
                        LOGGER_STATE.last_error = None
                    else:
                        current_epoch = now_epoch()
                        if target["status"] == SESSION_STATUS_SCHEDULED and current_epoch >= target["scheduled_start_epoch"]:
                            target = mark_session_running(conn, target["id"], current_epoch)
                            LOGGER_STATE.active_session_id = target["id"]

                        if target["status"] == SESSION_STATUS_RUNNING:
                            LOGGER_STATE.active_session_id = target["id"]
                            recovered_session = (
                                target["created_epoch"] < PROCESS_START_EPOCH
                                and target["id"] not in RECOVERY_SAMPLE_SESSION_IDS
                            )
                            if recovered_session:
                                sample_session(conn, target, current_epoch)
                                RECOVERY_SAMPLE_SESSION_IDS.add(target["id"])
                                target = fetch_session(conn, target["id"]) or target
                            else:
                                due_epoch = target["next_sample_epoch"] or target["actual_start_epoch"] or current_epoch
                                if current_epoch >= due_epoch:
                                    sample_session(conn, target, current_epoch)
                            LOGGER_STATE.last_error = None
                    LOGGER_STATE.last_cycle_epoch = now_epoch()
        except Exception as exc:  # pragma: no cover - background loop
            LOGGER_STATE.last_error = str(exc)
        time.sleep(1)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    active = None
    sessions: list[sqlite3.Row] = []
    chart_rows: list[dict[str, Any]] = []
    chart_session = None
    slot_rows: list[sqlite3.Row] = []
    db_error = INITIAL_DB_INIT_ERROR
    try:
        with db_session() as conn:
            active = fetch_active_or_scheduled(conn)
            sessions = fetch_sessions(conn, 20)
            active_id = active["id"] if active else (sessions[0]["id"] if sessions else None)
            chart_rows = fetch_chart_seed_rows(conn, active_id, 200) if active_id else []
            chart_session = serialize_session(fetch_session(conn, active_id)) if active_id else None
            slot_rows = fetch_sensor_slots(conn)
            db_error = None
    except Exception as exc:
        db_error = str(exc)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "default_poll_seconds": DEFAULT_POLL_SECONDS,
            "default_interval_seconds": DEFAULT_INTERVAL_SECONDS,
            "default_timezone_offset": DEFAULT_TIMEZONE_OFFSET,
            "default_timezone_location": DEFAULT_TIMEZONE_LOCATION,
            "default_fallback_temperature_c": DEFAULT_FALLBACK_TEMPERATURE_C,
            "max_sensor_slots": MAX_SENSOR_SLOTS,
            "active_session": serialize_session(active),
            "sessions": [serialize_session(row) for row in sessions],
            "chart_session": chart_session,
            "chart_seed_rows": chart_rows,
            "timezone_locations": build_timezone_location_options(now_epoch()),
            "sensor_slots": [
                {
                    "slot_no": row["slot_no"],
                    "enabled": bool(row["enabled"]),
                    "alias": row["alias"],
                    "sensor_id": row["sensor_id"],
                    "fallback_value_c": row["fallback_value_c"],
                }
                for row in slot_rows
            ],
            "initial_db_error": db_error,
        },
    )


@app.get("/api/health")
def health():
    storage = build_storage_status(write_heartbeat=False)
    return {
        "ok": storage.get("db_open_ok") and storage.get("directory_writable"),
        "scheduler_last_cycle_epoch": LOGGER_STATE.last_cycle_epoch,
        "scheduler_last_error": LOGGER_STATE.last_error,
        "active_session_id": LOGGER_STATE.active_session_id,
        "last_db_write_epoch": LOGGER_STATE.last_db_write_epoch,
        "storage": storage,
    }


@app.get("/api/timezone-locations")
def timezone_locations(reference_epoch: int | None = None):
    return {
        "default_timezone_location": DEFAULT_TIMEZONE_LOCATION,
        "items": build_timezone_location_options(reference_epoch or now_epoch()),
    }


@app.get("/api/status")
def status():
    current_epoch = now_epoch()
    detected_sensors: list[dict[str, Any]] = []
    sensor_error = None
    try:
        detected_sensors = read_all_sensors()
    except Exception as exc:
        sensor_error = str(exc)

    active = None
    latest_history: list[dict[str, Any]] = []
    slot_rows: list[sqlite3.Row] = []
    database_error = INITIAL_DB_INIT_ERROR
    try:
        with db_session() as conn:
            active = fetch_active_or_scheduled(conn)
            latest_history = fetch_latest_readings_for_session(conn, active["id"]) if active else []
            slot_rows = fetch_sensor_slots(conn)
            database_error = None
    except Exception as exc:
        database_error = str(exc)

    storage = build_storage_status(current_epoch=current_epoch, write_heartbeat=False)
    if database_error and not storage.get("db_open_error"):
        storage["db_open_error"] = database_error
        storage["summary_level"] = "bad"
        storage["summary"] = database_error

    return {
        "host_name": socket.gethostname(),
        "host_time_epoch": current_epoch,
        "host_time_utc": format_epoch(current_epoch, 0),
        "configured_poll_seconds": DEFAULT_POLL_SECONDS,
        "sensor_count": len(list_sensors()),
        "detected_sensors": detected_sensors,
        "sensor_error": sensor_error,
        "configured_slots": build_runtime_sensor_slots(slot_rows, detected_sensors, latest_history) if slot_rows else [],
        "active_session": serialize_session(active),
        "active_session_latest_readings": latest_history,
        "scheduler_error": LOGGER_STATE.last_error,
        "database_error": database_error,
        "disk": build_disk_stats(),
        "storage": storage,
        "network": network_manager_status(),
    }


@app.get("/api/sensors")
def sensors():
    readings = read_all_sensors()
    return {"count": len(readings), "items": readings}


@app.get("/api/sensor-slots")
def sensor_slots():
    with db_session() as conn:
        slot_rows = fetch_sensor_slots(conn)
        detected_sensors = read_all_sensors()
        return {
            "max_sensor_slots": MAX_SENSOR_SLOTS,
            "items": build_runtime_sensor_slots(slot_rows, detected_sensors),
            "detected_sensors": detected_sensors,
        }


@app.put("/api/sensor-slots")
def update_sensor_slots(payload: SensorSlotsUpdatePayload):
    with LOGGER_LOCK:
        with db_session() as conn:
            rows = save_sensor_slots(conn, payload)
            detected_sensors = read_all_sensors()
            return {
                "message": "Sensor slots updated.",
                "items": build_runtime_sensor_slots(rows, detected_sensors),
            }


@app.get("/api/network")
def network_status():
    return network_manager_status()


@app.post("/api/network/wired")
def update_wired_network(payload: WiredNetworkPayload):
    try:
        return {
            "message": "Wired network settings applied.",
            "network": apply_wired_network(
                mode=payload.mode,
                ip_cidr=payload.ip_cidr,
                gateway=payload.gateway,
                dns=payload.dns,
            ),
        }
    except NetworkUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except NetworkConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/sessions")
def sessions(limit: int = 50):
    with db_session() as conn:
        rows = fetch_sessions(conn, max(1, min(limit, 500)))
        return {"items": [serialize_session(item) for item in rows]}


@app.post("/api/sessions")
def start_session(payload: StartSessionPayload):
    with LOGGER_LOCK:
        with db_session() as conn:
            session_row = create_session_record(conn, payload)
            return {"message": "Logging started.", "session": serialize_session(session_row)}


@app.post("/api/sessions/{session_id}/stop")
def stop_session(session_id: int):
    with LOGGER_LOCK:
        with db_session() as conn:
            session_row = stop_session_record(conn, session_id)
            return {"message": "Session stopped.", "session": serialize_session(session_row)}


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: int):
    with LOGGER_LOCK:
        with db_session() as conn:
            delete_session_record(conn, session_id)
            return {"message": "Session deleted."}


@app.get("/api/sessions/{session_id}/readings")
def session_readings(session_id: int, limit: int = 500):
    with db_session() as conn:
        session_row = fetch_session(conn, session_id)
        if not session_row:
            raise HTTPException(status_code=404, detail="Session not found.")
        items = fetch_reading_items(conn, session_row, max(1, min(limit, 5000)))
        return {
            "session": serialize_session(session_row),
            "items": items,
        }


@app.get("/api/sessions/{session_id}/export")
def export_session(session_id: int, format: str = "csv"):
    export_format = format.lower()
    if export_format not in ALLOWED_EXPORTS:
        raise HTTPException(status_code=400, detail=f"format must be one of {sorted(ALLOWED_EXPORTS)}")
    with db_session() as conn:
        session_row = fetch_session(conn, session_id)
        if not session_row:
            raise HTTPException(status_code=404, detail="Session not found.")
        output_path = build_export(conn, session_row, export_format)

    media_types = {
        "csv": "text/csv",
        "txt": "text/plain",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    return FileResponse(
        path=output_path,
        media_type=media_types[export_format],
        filename=output_path.name,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):  # pragma: no cover - framework glue
    return JSONResponse(status_code=500, content={"detail": str(exc)})


def resolve_ssl_files() -> tuple[str | None, str | None]:
    cert_path = Path(HTTPS_CERTFILE) if HTTPS_CERTFILE else None
    key_path = Path(HTTPS_KEYFILE) if HTTPS_KEYFILE else None
    if cert_path and key_path and cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)
    return None, None


def main() -> None:
    ssl_certfile, ssl_keyfile = resolve_ssl_files()
    uvicorn.run(
        "app.main:app",
        host=APP_HOST,
        port=APP_PORT,
        reload=False,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )


if __name__ == "__main__":
    main()
