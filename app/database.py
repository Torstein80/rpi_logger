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


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _ensure_logging_sessions(conn: sqlite3.Connection) -> None:
    conn.execute(
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
        )
        """
    )


def _ensure_temperature_readings_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
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
        )
        """
    )


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

    # Backfill from older schemas that used slot_number.
    columns = _column_names(conn, "temperature_readings")
    if "slot_no" in columns and "slot_number" in columns:
        conn.execute(
            "UPDATE temperature_readings SET slot_no = COALESCE(slot_no, slot_number)"
        )

    conn.execute(
        """
        UPDATE temperature_readings
        SET slot_no = COALESCE(slot_no, 0),
            status = COALESCE(NULLIF(status, ''), 'ok'),
            is_substituted = COALESCE(is_substituted, 0)
        """
    )


def _migrate_sensor_slots_from_slot_number(conn: sqlite3.Connection) -> None:
    columns = _column_names(conn, "sensor_slots")
    if "slot_no" in columns or "slot_number" not in columns:
        return

    conn.execute(
        """
        CREATE TABLE sensor_slots_new (
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
    conn.execute(
        """
        INSERT INTO sensor_slots_new (slot_no, enabled, alias, sensor_id, fallback_value_c, updated_epoch)
        SELECT slot_number,
               COALESCE(enabled, 0),
               COALESCE(alias, ''),
               sensor_id,
               COALESCE(fallback_value_c, ?),
               COALESCE(updated_epoch, 0)
        FROM sensor_slots
        """,
        (DEFAULT_FALLBACK_TEMPERATURE_C,),
    )
    conn.execute("DROP TABLE sensor_slots")
    conn.execute("ALTER TABLE sensor_slots_new RENAME TO sensor_slots")



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

    _migrate_sensor_slots_from_slot_number(conn)

    columns = _column_names(conn, "sensor_slots")
    if "enabled" not in columns:
        conn.execute("ALTER TABLE sensor_slots ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0")
    if "alias" not in columns:
        conn.execute("ALTER TABLE sensor_slots ADD COLUMN alias TEXT NOT NULL DEFAULT ''")
    if "sensor_id" not in columns:
        conn.execute("ALTER TABLE sensor_slots ADD COLUMN sensor_id TEXT")
    if "fallback_value_c" not in columns:
        conn.execute(
            f"ALTER TABLE sensor_slots ADD COLUMN fallback_value_c REAL NOT NULL DEFAULT {DEFAULT_FALLBACK_TEMPERATURE_C}"
        )
    if "updated_epoch" not in columns:
        conn.execute("ALTER TABLE sensor_slots ADD COLUMN updated_epoch INTEGER NOT NULL DEFAULT 0")

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



def _ensure_indexes(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status ON logging_sessions(status)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_readings_session_epoch ON temperature_readings(session_id, sample_epoch)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_readings_sensor_epoch ON temperature_readings(sensor_id, sample_epoch)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_readings_session_slot_epoch ON temperature_readings(session_id, slot_no, sample_epoch)"
    )



def init_db() -> None:
    with get_connection() as conn:
        _ensure_logging_sessions(conn)
        _ensure_temperature_readings_table(conn)
        _ensure_temperature_reading_columns(conn)
        _ensure_sensor_slots(conn)
        _ensure_indexes(conn)


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
