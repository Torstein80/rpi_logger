from __future__ import annotations

import shutil
import socket
import sqlite3
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
)
from .database import db_session, init_db
from .exporters import build_export
from .models import SESSION_STATUS_COMPLETED, SESSION_STATUS_RUNNING, SESSION_STATUS_SCHEDULED
from .sensors import list_sensors, read_all_sensors, read_configured_sensor
from .utils import (
    DEFAULT_TIMEZONE_LOCATION,
    build_timezone_location_options,
    format_epoch,
    offset_minutes_to_text,
    parse_timezone_offset,
)


init_db()


@dataclass
class LoggerState:
    active_session_id: int | None = None
    last_cycle_epoch: int | None = None
    last_error: str | None = None


LOGGER_STATE = LoggerState()
LOGGER_LOCK = threading.Lock()


def now_epoch() -> int:
    return int(time.time())


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


def fetch_chart_seed_rows(conn: sqlite3.Connection, session_id: int, limit: int = 200) -> list[dict[str, Any]]:
    rows = fetch_readings(conn, session_id, limit)
    return [
        {
            "sample_epoch": row["sample_epoch"],
            "slot_no": row["slot_no"],
            "sensor_id": row["sensor_id"],
            "sensor_name": row["sensor_name"],
            "temperature_c": row["temperature_c"],
            "status": row["status"],
            "is_substituted": bool(row["is_substituted"]),
            "error_text": row["error_text"],
        }
        for row in rows
    ]


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
    usage = shutil.disk_usage(str(db_path.parent))
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
    with db_session() as conn:
        active = fetch_active_or_scheduled(conn)
        sessions = fetch_sessions(conn, 20)
        active_id = active["id"] if active else (sessions[0]["id"] if sessions else None)
        chart_rows = fetch_chart_seed_rows(conn, active_id, 200) if active_id else []
        chart_session = serialize_session(fetch_session(conn, active_id)) if active_id else None
        slot_rows = fetch_sensor_slots(conn)
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
        },
    )


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "scheduler_last_cycle_epoch": LOGGER_STATE.last_cycle_epoch,
        "scheduler_last_error": LOGGER_STATE.last_error,
        "active_session_id": LOGGER_STATE.active_session_id,
    }


@app.get("/api/timezone-locations")
def timezone_locations(reference_epoch: int | None = None):
    return {
        "default_timezone_location": DEFAULT_TIMEZONE_LOCATION,
        "items": build_timezone_location_options(reference_epoch or now_epoch()),
    }


@app.get("/api/status")
def status():
    with db_session() as conn:
        active = fetch_active_or_scheduled(conn)
        latest_history = fetch_latest_readings_for_session(conn, active["id"]) if active else []
        detected_sensors = read_all_sensors()
        slot_rows = fetch_sensor_slots(conn)
        current_epoch = now_epoch()
        return {
            "host_name": socket.gethostname(),
            "host_time_epoch": current_epoch,
            "host_time_utc": format_epoch(current_epoch, 0),
            "configured_poll_seconds": DEFAULT_POLL_SECONDS,
            "sensor_count": len(list_sensors()),
            "detected_sensors": detected_sensors,
            "configured_slots": build_runtime_sensor_slots(slot_rows, detected_sensors, latest_history),
            "active_session": serialize_session(active),
            "active_session_latest_readings": latest_history,
            "scheduler_error": LOGGER_STATE.last_error,
            "disk": build_disk_stats(),
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
        rows = fetch_readings(conn, session_id, max(1, min(limit, 5000)))
        return {
            "session": serialize_session(session_row),
            "items": [
                {
                    "sample_epoch": row["sample_epoch"],
                    "sample_time_local": format_epoch(row["sample_epoch"], session_row["timezone_offset_minutes"]),
                    "slot_no": row["slot_no"],
                    "sensor_id": row["sensor_id"],
                    "sensor_name": row["sensor_name"],
                    "temperature_c": round(row["temperature_c"], 4),
                    "status": row["status"],
                    "is_substituted": bool(row["is_substituted"]),
                    "error_text": row["error_text"],
                }
                for row in rows
            ],
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
