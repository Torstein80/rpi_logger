from __future__ import annotations

import sqlite3
import threading
import time
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
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_POLL_SECONDS,
    DEFAULT_TIMEZONE_OFFSET,
    HTTPS_CERTFILE,
    HTTPS_KEYFILE,
)
from .database import db_session, init_db
from .exporters import build_export
from .models import SESSION_STATUS_COMPLETED, SESSION_STATUS_RUNNING, SESSION_STATUS_SCHEDULED
from .sensors import list_sensors, read_all_sensors
from .utils import format_epoch, offset_minutes_to_text, parse_timezone_offset


init_db()

app = FastAPI(title="Haga RPI Temperature Logger")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class StartSessionPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    start_epoch: int = Field(ge=0)
    interval_seconds: int = Field(ge=1, le=3600)
    timezone_offset: str = Field(default=DEFAULT_TIMEZONE_OFFSET)


@dataclass
class LoggerState:
    active_session_id: int | None = None
    last_cycle_epoch: int | None = None
    last_error: str | None = None


LOGGER_STATE = LoggerState()
LOGGER_LOCK = threading.Lock()


def now_epoch() -> int:
    return int(time.time())


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


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
    return conn.execute(
        "SELECT * FROM logging_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()


def fetch_latest_readings_for_session(conn: sqlite3.Connection, session_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT r.sensor_id, r.sensor_name, r.temperature_c, r.sample_epoch
        FROM temperature_readings r
        JOIN (
            SELECT sensor_id, MAX(sample_epoch) AS max_epoch
            FROM temperature_readings
            WHERE session_id = ?
            GROUP BY sensor_id
        ) latest
          ON latest.sensor_id = r.sensor_id
         AND latest.max_epoch = r.sample_epoch
        WHERE r.session_id = ?
        ORDER BY r.sensor_id ASC
        """,
        (session_id, session_id),
    ).fetchall()
    return [
        {
            "sensor_id": row["sensor_id"],
            "sensor_name": row["sensor_name"],
            "temperature_c": round(row["temperature_c"], 3),
            "sample_epoch": row["sample_epoch"],
        }
        for row in rows
    ]


def fetch_readings(conn: sqlite3.Connection, session_id: int, limit: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT sample_epoch, sensor_id, sensor_name, temperature_c
        FROM temperature_readings
        WHERE session_id = ?
        ORDER BY sample_epoch DESC, sensor_id ASC
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
            "sensor_id": row["sensor_id"],
            "sensor_name": row["sensor_name"],
            "temperature_c": row["temperature_c"],
        }
        for row in rows
    ]


def create_session_record(conn: sqlite3.Connection, payload: StartSessionPayload) -> sqlite3.Row:
    offset_minutes = parse_timezone_offset(payload.timezone_offset)
    current_epoch = now_epoch()
    existing = fetch_active_or_scheduled(conn)
    if existing:
        raise HTTPException(status_code=409, detail="A session is already scheduled or running.")

    status = SESSION_STATUS_SCHEDULED if payload.start_epoch > current_epoch else SESSION_STATUS_RUNNING
    actual_start_epoch = None if status == SESSION_STATUS_SCHEDULED else current_epoch
    next_sample_epoch = payload.start_epoch if status == SESSION_STATUS_SCHEDULED else current_epoch

    cursor = conn.execute(
        """
        INSERT INTO logging_sessions (
            name, scheduled_start_epoch, actual_start_epoch, stop_epoch, interval_seconds,
            timezone_offset_minutes, status, created_epoch, next_sample_epoch
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)
        """,
        (
            payload.name.strip(),
            payload.start_epoch,
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


def sample_session(conn: sqlite3.Connection, session_row: sqlite3.Row, sample_epoch: int) -> None:
    sensor_readings = read_all_sensors()
    if not sensor_readings:
        raise RuntimeError("No 1-Wire sensors detected in /sys/bus/w1/devices.")
    for reading in sensor_readings:
        conn.execute(
            """
            INSERT INTO temperature_readings (session_id, sample_epoch, sensor_id, sensor_name, temperature_c)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_row["id"],
                sample_epoch,
                reading["sensor_id"],
                reading["sensor_name"],
                reading["temperature_c"],
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


@app.on_event("startup")
def startup_event() -> None:
    threading.Thread(target=scheduler_loop, daemon=True, name="logger-scheduler").start()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with db_session() as conn:
        active = fetch_active_or_scheduled(conn)
        sessions = fetch_sessions(conn, 20)
        active_id = active["id"] if active else (sessions[0]["id"] if sessions else None)
        chart_rows = fetch_chart_seed_rows(conn, active_id, 200) if active_id else []
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "default_poll_seconds": DEFAULT_POLL_SECONDS,
            "default_interval_seconds": DEFAULT_INTERVAL_SECONDS,
            "default_timezone_offset": DEFAULT_TIMEZONE_OFFSET,
            "active_session": serialize_session(active),
            "sessions": [serialize_session(row) for row in sessions],
            "chart_session_id": active_id,
            "chart_seed_rows": chart_rows,
        },
    )


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "scheduler_last_cycle_epoch": LOGGER_STATE.last_cycle_epoch,
        "scheduler_last_error": LOGGER_STATE.last_error,
    }


@app.get("/api/status")
def status():
    with db_session() as conn:
        active = fetch_active_or_scheduled(conn)
        latest_history = fetch_latest_readings_for_session(conn, active["id"]) if active else None
        current_sensors = []
        sensor_error = None
        try:
            current_sensors = read_all_sensors()
        except Exception as exc:
            sensor_error = str(exc)

        current_epoch = now_epoch()
        return {
            "host_time_epoch": current_epoch,
            "host_time_utc": format_epoch(current_epoch, 0),
            "configured_poll_seconds": DEFAULT_POLL_SECONDS,
            "sensor_count": len(list_sensors()),
            "current_sensors": current_sensors,
            "sensor_error": sensor_error,
            "active_session": serialize_session(active),
            "active_session_latest_readings": latest_history,
            "scheduler_error": LOGGER_STATE.last_error,
        }


@app.get("/api/sensors")
def sensors():
    try:
        readings = read_all_sensors()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"count": len(readings), "items": readings}


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
            return {"message": "Session created.", "session": serialize_session(session_row)}


@app.post("/api/sessions/{session_id}/stop")
def stop_session(session_id: int):
    with LOGGER_LOCK:
        with db_session() as conn:
            session_row = stop_session_record(conn, session_id)
            return {"message": "Session stopped.", "session": serialize_session(session_row)}


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
                    "sensor_id": row["sensor_id"],
                    "sensor_name": row["sensor_name"],
                    "temperature_c": round(row["temperature_c"], 4),
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


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=APP_HOST,
        port=APP_PORT,
        reload=False,
        ssl_certfile=HTTPS_CERTFILE or None,
        ssl_keyfile=HTTPS_KEYFILE or None,
    )


if __name__ == "__main__":
    main()
