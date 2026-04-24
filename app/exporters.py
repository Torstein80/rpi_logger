from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from openpyxl import Workbook

from .config import EXPORT_DIR
from .utils import ensure_dir, format_epoch, offset_minutes_to_text, safe_filename


def augment_rows_with_gap_markers(rows: list[dict], interval_seconds: int, timezone_offset_minutes: int) -> list[dict]:
    if not rows or interval_seconds <= 0:
        return rows

    grouped: dict[int | str, list[dict]] = {}
    for row in rows:
        key = row.get("slot_no") or row.get("sensor_id") or "unknown"
        grouped.setdefault(key, []).append(dict(row))

    gap_threshold = max(int(interval_seconds * 1.5), interval_seconds + 1)
    merged: list[dict] = []
    for group_rows in grouped.values():
        group_rows.sort(key=lambda item: (int(item["sample_epoch"]), int(item.get("slot_no") or 0)))
        previous: dict | None = None
        for current in group_rows:
            if previous is not None:
                delta = int(current["sample_epoch"]) - int(previous["sample_epoch"])
                if delta > gap_threshold:
                    gap_epoch = int(previous["sample_epoch"]) + interval_seconds
                    offline_seconds = max(int(current["sample_epoch"]) - gap_epoch, 0)
                    resume_local = format_epoch(int(current["sample_epoch"]), timezone_offset_minutes)
                    merged.append(
                        {
                            "session_name": current.get("session_name", ""),
                            "session_id": current.get("session_id"),
                            "sample_epoch": gap_epoch,
                            "sample_time_local": format_epoch(gap_epoch, timezone_offset_minutes),
                            "timezone_offset": offset_minutes_to_text(timezone_offset_minutes),
                            "slot_no": current.get("slot_no") or previous.get("slot_no"),
                            "sensor_id": current.get("sensor_id") or previous.get("sensor_id"),
                            "sensor_name": current.get("sensor_name") or previous.get("sensor_name"),
                            "temperature_c": "",
                            "status": "offline_gap",
                            "is_substituted": 1,
                            "error_text": f"Logger offline or unavailable for {offline_seconds} s. Logging resumed at {resume_local}.",
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


def build_export(conn: sqlite3.Connection, session_row: sqlite3.Row, export_format: str) -> Path:
    ensure_dir(EXPORT_DIR)
    file_name = safe_filename(
        session_name=session_row["name"],
        start_epoch=session_row["actual_start_epoch"] or session_row["scheduled_start_epoch"],
        stop_epoch=session_row["stop_epoch"],
        extension=export_format,
    )
    output_path = Path(EXPORT_DIR) / file_name

    readings = conn.execute(
        """
        SELECT sample_epoch, slot_no, sensor_id, sensor_name, temperature_c, status, is_substituted, error_text
        FROM temperature_readings
        WHERE session_id = ?
        ORDER BY sample_epoch ASC, slot_no ASC, sensor_id ASC
        """,
        (session_row["id"],),
    ).fetchall()

    rows = []
    for item in readings:
        rows.append(
            {
                "session_name": session_row["name"],
                "session_id": session_row["id"],
                "sample_epoch": item["sample_epoch"],
                "sample_time_local": format_epoch(item["sample_epoch"], session_row["timezone_offset_minutes"]),
                "timezone_offset": offset_minutes_to_text(session_row["timezone_offset_minutes"]),
                "slot_no": item["slot_no"],
                "sensor_id": item["sensor_id"],
                "sensor_name": item["sensor_name"],
                "temperature_c": round(item["temperature_c"], 4),
                "status": item["status"],
                "is_substituted": int(item["is_substituted"] or 0),
                "error_text": item["error_text"] or "",
            }
        )

    rows = augment_rows_with_gap_markers(
        rows,
        session_row["interval_seconds"],
        session_row["timezone_offset_minutes"],
    )

    if export_format == "csv":
        _write_csv(output_path, rows)
    elif export_format == "txt":
        _write_txt(output_path, rows, session_row)
    elif export_format == "xlsx":
        _write_xlsx(output_path, rows, session_row)
    else:
        raise ValueError(f"Unsupported export format: {export_format}")
    return output_path


def _default_headers() -> list[str]:
    return [
        "session_name",
        "session_id",
        "sample_epoch",
        "sample_time_local",
        "timezone_offset",
        "slot_no",
        "sensor_id",
        "sensor_name",
        "temperature_c",
        "status",
        "is_substituted",
        "error_text",
    ]


def _write_csv(output_path: Path, rows: list[dict]) -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else _default_headers())
        writer.writeheader()
        writer.writerows(rows)


def _write_txt(output_path: Path, rows: list[dict], session_row: sqlite3.Row) -> None:
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(f"Session: {session_row['name']}\n")
        handle.write(f"Session ID: {session_row['id']}\n")
        handle.write(f"Status: {session_row['status']}\n")
        handle.write(f"Scheduled start epoch: {session_row['scheduled_start_epoch']}\n")
        handle.write(f"Actual start epoch: {session_row['actual_start_epoch']}\n")
        handle.write(f"Stop epoch: {session_row['stop_epoch']}\n")
        handle.write(f"Interval seconds: {session_row['interval_seconds']}\n")
        handle.write(f"Timezone offset: {offset_minutes_to_text(session_row['timezone_offset_minutes'])}\n")
        handle.write("\n")
        handle.write(
            "sample_epoch\tsample_time_local\tslot_no\tsensor_id\tsensor_name\ttemperature_c\tstatus\tis_substituted\terror_text\n"
        )
        for row in rows:
            handle.write(
                f"{row['sample_epoch']}\t{row['sample_time_local']}\t{row['slot_no']}\t{row['sensor_id']}\t"
                f"{row['sensor_name']}\t{row['temperature_c']}\t{row['status']}\t{row['is_substituted']}\t"
                f"{row['error_text']}\n"
            )


def _write_xlsx(output_path: Path, rows: list[dict], session_row: sqlite3.Row) -> None:
    wb = Workbook()
    ws_meta = wb.active
    ws_meta.title = "session"
    ws_meta.append(["field", "value"])
    ws_meta.append(["session_name", session_row["name"]])
    ws_meta.append(["session_id", session_row["id"]])
    ws_meta.append(["status", session_row["status"]])
    ws_meta.append(["scheduled_start_epoch", session_row["scheduled_start_epoch"]])
    ws_meta.append(["actual_start_epoch", session_row["actual_start_epoch"]])
    ws_meta.append(["stop_epoch", session_row["stop_epoch"]])
    ws_meta.append(["interval_seconds", session_row["interval_seconds"]])
    ws_meta.append(["timezone_offset", offset_minutes_to_text(session_row["timezone_offset_minutes"])])

    ws_data = wb.create_sheet("readings")
    headers = list(rows[0].keys()) if rows else _default_headers()
    ws_data.append(headers)
    for row in rows:
        ws_data.append([row[column] for column in headers])

    for sheet in (ws_meta, ws_data):
        for column_cells in sheet.columns:
            max_length = max(len("" if cell.value is None else str(cell.value)) for cell in column_cells)
            sheet.column_dimensions[column_cells[0].column_letter].width = min(max_length + 2, 40)

    wb.save(output_path)
