from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


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
                sensor_id TEXT NOT NULL,
                sensor_name TEXT NOT NULL,
                temperature_c REAL NOT NULL,
                FOREIGN KEY (session_id) REFERENCES logging_sessions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_status ON logging_sessions(status);
            CREATE INDEX IF NOT EXISTS idx_readings_session_epoch ON temperature_readings(session_id, sample_epoch);
            CREATE INDEX IF NOT EXISTS idx_readings_sensor_epoch ON temperature_readings(sensor_id, sample_epoch);
            """
        )


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
