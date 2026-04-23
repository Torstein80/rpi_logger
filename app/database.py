from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager

from .config import DB_PATH, DEFAULT_FALLBACK_TEMPERATURE_C, MAX_SENSOR_SLOTS

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _column_names(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def _ensure_temperature_reading_columns(conn: sqlite3.Connection) -> None:
    columns = _column_names(conn, "temperature_readings")
    statements: list[str] = []
    if "slot_no" not in columns:
        statements.append("ALTER TABLE temperature_readings ADD COLUMN slot_no INTEGER")
    if "status" not in columns:
        statements.append("ALTER TABLE temperature_readings ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'")
    if "is_substituted" not in columns:
        statements.append("ALTER TABLE temperature_readings ADD COLUMN is_substituted INTEGER NOT NULL DEFAULT 0")
    if "error_text" not in columns:
        statements.append("ALTER TABLE temperature_readings ADD COLUMN error_text TEXT")

    for statement in statements:
        conn.execute(statement)

    # Backfill older rows to make slot-based history work better.
    conn.execute(
        """
        UPDATE temperature_readings
        SET slot_no = COALESCE(slot_no, 0),
            status = COALESCE(NULLIF(status, ''), 'ok'),
            is_substituted = COALESCE(is_substituted, 0)
        """
    )


def _ensure_sensor_slots(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sensor_slots (
            slot_no INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            alias TEXT NOT NULL DEFAULT '',
            sensor_id TEXT,
            fallback_value_c REAL NOT NULL DEFAULT 85.0,
            updated_epoch INTEGER NOT NULL DEFAULT 0,
            CHECK (slot_no >= 1 AND slot_no <= 4)
        )
        """
    )
    current_epoch = int(time.time())
    for slot_no in range(1, MAX_SENSOR_SLOTS + 1):
        conn.execute(
            """
            INSERT OR IGNORE INTO sensor_slots (
                slot_no, enabled, alias, sensor_id, fallback_value_c, updated_epoch
            ) VALUES (?, 0, '', NULL, ?, ?)
            """,
            (slot_no, DEFAULT_FALLBACK_TEMPERATURE_C, current_epoch),
        )


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS logging_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                scheduled_start_epoch INTEGER NOT NULL,
                actual_start_epoch INTEGER,
                stop_epoch INTEGER,
                interval_seconds INTEGER NOT NULL,
                timezone_offset_minutes INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'scheduled',
                created_epoch INTEGER NOT NULL,
                next_sample_epoch INTEGER
            );

            CREATE TABLE IF NOT EXISTS temperature_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                sample_epoch INTEGER NOT NULL,
                slot_no INTEGER,
                sensor_id TEXT NOT NULL,
                sensor_name TEXT NOT NULL,
                temperature_c REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'ok',
                is_substituted INTEGER NOT NULL DEFAULT 0,
                error_text TEXT,
                FOREIGN KEY (session_id) REFERENCES logging_sessions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_status ON logging_sessions(status);
            CREATE INDEX IF NOT EXISTS idx_readings_session_epoch ON temperature_readings(session_id, sample_epoch);
            CREATE INDEX IF NOT EXISTS idx_readings_sensor_epoch ON temperature_readings(sensor_id, sample_epoch);
            CREATE INDEX IF NOT EXISTS idx_readings_session_slot_epoch ON temperature_readings(session_id, slot_no, sample_epoch);
            """
        )
        _ensure_temperature_reading_columns(conn)
        _ensure_sensor_slots(conn)


@contextmanager
def db_session():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
