from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASKS_FILE = ROOT / "data" / "tasks.json"
CONTROL_DB = ROOT / "data" / "send_control.sqlite3"


def task_db_paths(current_db_path: str = "") -> list[Path]:
    paths: list[Path] = []
    if TASKS_FILE.exists():
        try:
            tasks = json.loads(TASKS_FILE.read_text(encoding="utf-8") or "[]")
        except (json.JSONDecodeError, OSError):
            tasks = []
        for task in tasks:
            value = str(task.get("db_path", "")).strip()
            if value:
                path = Path(value)
                paths.append(path if path.is_absolute() else ROOT / path)
    if current_db_path:
        current = Path(current_db_path)
        paths.append(current if current.is_absolute() else ROOT / current)

    unique: list[Path] = []
    seen = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            unique.append(resolved)
            seen.add(resolved)
    return unique


def global_sent_state(current_db_path: str = "") -> tuple[int, float | None]:
    sent_today = 0
    last_sent_at: float | None = None
    for db_path in task_db_paths(current_db_path):
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(str(db_path), timeout=10)
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN date(sent_at, 'localtime') = date('now', 'localtime') THEN 1 ELSE 0 END),
                    MAX(strftime('%s', sent_at))
                FROM email_drafts
                WHERE sent_at IS NOT NULL
                """
            ).fetchone()
            conn.close()
        except sqlite3.Error:
            continue
        sent_today += int((row[0] if row else 0) or 0)
        if row and row[1] is not None:
            timestamp = float(row[1])
            last_sent_at = timestamp if last_sent_at is None else max(last_sent_at, timestamp)
    return sent_today, last_sent_at


@dataclass
class SendSlot:
    connection: sqlite3.Connection

    def release(self) -> None:
        try:
            self.connection.commit()
        finally:
            self.connection.close()


def acquire_send_slot(
    current_db_path: str,
    delay_seconds: float,
    daily_limit: int,
    continue_after_sleep: bool = False,
) -> tuple[SendSlot | None, str]:
    CONTROL_DB.parent.mkdir(parents=True, exist_ok=True)
    while True:
        control = sqlite3.connect(str(CONTROL_DB), timeout=90, isolation_level=None)
        control.execute(
            "CREATE TABLE IF NOT EXISTS send_control (id INTEGER PRIMARY KEY CHECK (id = 1), updated_at TEXT)"
        )
        control.execute("BEGIN IMMEDIATE")
        sent_today, last_sent_at = global_sent_state(current_db_path)
        if sent_today >= daily_limit:
            control.rollback()
            control.close()
            return None, f"Global daily send limit reached: {sent_today}/{daily_limit} sent today across all tasks."

        remaining = 0.0
        if last_sent_at is not None and delay_seconds > 0:
            remaining = delay_seconds - (time.time() - last_sent_at)
        if remaining <= 0:
            control.execute(
                "INSERT INTO send_control (id, updated_at) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),),
            )
            return SendSlot(control), ""

        control.rollback()
        control.close()
        if not sleep_safely(remaining, continue_after_sleep=continue_after_sleep):
            return None, "Global send interval wait was interrupted or suspended."


def sleep_safely(delay: float, continue_after_sleep: bool = False) -> bool:
    started_monotonic = time.monotonic()
    started_wall = time.time()
    deadline = started_monotonic + delay
    print(f"Waiting {delay:g}s for the shared multi-task send interval.", flush=True)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 60))
    wall_elapsed = time.time() - started_wall
    if wall_elapsed > delay + 30:
        print(
            f"Shared send wait interrupted: wall-clock wait was {wall_elapsed:.0f}s; expected {delay:g}s.",
            flush=True,
        )
        return continue_after_sleep
    return True
