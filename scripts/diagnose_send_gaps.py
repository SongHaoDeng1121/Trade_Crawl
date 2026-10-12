#!/usr/bin/env python3
"""Read-only send-gap report for a Trade Crawl installation on macOS."""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path


EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
SECRET = re.compile(r"(?i)(api[_-]?key|password|token|authorization)(\s*[=:]\s*)\S+")
URL = re.compile(r"https?://\S+", re.I)
LOG_MARKERS = (
    "queue cycle", "queue sleeping", "global daily send", "shared send wait",
    "wait interrupted", "wait finished", "waiting ", "processed ",
    "sending draft", "sent draft", "send or local status update failed",
    "stopping send batch", "returned exit code", "failed:",
    "real sending is", "queue intervals", "starting auto_pipeline",
    "job finished", "no emails were sent", "skipped ",
)


def redact(value: str) -> str:
    value = EMAIL.sub("[email hidden]", value)
    value = SECRET.sub(r"\1\2[hidden]", value)
    value = URL.sub("[url hidden]", value)
    return value[:600]


def command(args: list[str], timeout: int = 30) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.stdout if result.returncode == 0 else f"unavailable (exit {result.returncode})"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable ({type(exc).__name__})"


def local_time(utc_text: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(utc_text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone()
    except ValueError:
        return None


def report(root: Path, since: dt.date) -> str:
    lines: list[str] = []

    def add(*values: object) -> None:
        lines.extend(str(value) for value in values)

    add("TRADE CRAWL SEND DIAGNOSTIC (read-only)")
    add(f"Generated: {dt.datetime.now().astimezone().isoformat(timespec='seconds')}")
    add(f"Project: {root}")
    add(f"Since (local date): {since}")
    add(f"Python: {sys.version.split()[0]}")
    add(f"macOS: {command(['sw_vers', '-productVersion']).strip()}")
    add(f"Architecture: {command(['uname', '-m']).strip()}")
    add(f"Hardware model: {command(['sysctl', '-n', 'hw.model']).strip()}")
    add(f"RAM bytes: {command(['sysctl', '-n', 'hw.memsize']).strip()}")
    add(f"Uptime: {command(['uptime']).strip()}")
    add(f"Disk free: {command(['df', '-h', str(root)]).splitlines()[-1]}")
    add("Code fingerprints:")
    for name in ("cli.py", "send_control.py", "dashboard.py"):
        path = root / "trade_outreach" / name
        if path.is_file():
            add(f"  {name}: {hashlib.sha256(path.read_bytes()).hexdigest()[:16]}")

    add("\nSAFE CONFIG VALUES")
    safe_keys = {
        "SEND_DELAY_SECONDS", "SEND_DAILY_LIMIT", "AUTO_SEND_IDLE_SECONDS",
        "AUTO_PROCESSING_INTERVAL_SECONDS", "AUTO_DISCOVERY_INTERVAL_SECONDS",
        "SMTP_SECURITY", "SMTP_PORT",
    }
    env_file = root / ".env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in raw or raw.lstrip().startswith("#"):
                continue
            key, value = raw.split("=", 1)
            if key.strip() in safe_keys:
                add(f"{key.strip()}={redact(value.strip())}")
    else:
        add(".env not found")
    add("Dashboard commands specify 800 seconds for real send and auto-pipeline in this code version.")

    add("\nSENT EMAILS AND GAPS (SQLite timestamps converted from UTC to local time)")
    tasks_file = root / "data" / "tasks.json"
    try:
        tasks = json.loads(tasks_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tasks = []
        add("tasks.json unavailable")
    records: list[tuple[dt.datetime, str]] = []
    for task in tasks:
        name = str(task.get("name") or task.get("id") or "unknown")
        path = Path(str(task.get("db_path", "")))
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            add(f"{name}: database missing")
            continue
        try:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            sent = conn.execute("SELECT sent_at FROM email_drafts WHERE sent_at IS NOT NULL").fetchall()
            queue = conn.execute(
                "SELECT COUNT(*) FROM email_drafts WHERE approved=1 AND sent_at IS NULL"
            ).fetchone()[0]
            conn.close()
        except sqlite3.Error as exc:
            add(f"{name}: database read error: {type(exc).__name__}")
            continue
        recent = [(local_time(row[0]), name) for row in sent]
        recent = [(when, task_name) for when, task_name in recent if when and when.date() >= since]
        records.extend(recent)
        add(f"{name}: approved unsent={queue}, sent since {since}={len(recent)}")
    records.sort()
    by_day: dict[str, list[dt.datetime]] = collections.defaultdict(list)
    for when, _ in records:
        by_day[str(when.date())].append(when)
    for day, times in sorted(by_day.items()):
        add(f"{day}: {len(times)} sent; first={times[0]:%H:%M:%S}, last={times[-1]:%H:%M:%S}")
    add("Gaps over 30 minutes between ALL tasks (no email addresses shown):")
    long_gaps = 0
    for previous, current in zip(records, records[1:]):
        minutes = (current[0] - previous[0]).total_seconds() / 60
        if minutes >= 30:
            long_gaps += 1
            add(
                f"  {previous[0]:%Y-%m-%d %H:%M:%S} {previous[1]} -> "
                f"{current[0]:%Y-%m-%d %H:%M:%S} {current[1]}: {minutes:.1f} min"
            )
    add(f"Total gaps over 30 minutes: {long_gaps}")

    add("\nDASHBOARD JOB RECORDS")
    jobs_file = root / "data" / "dashboard_jobs.json"
    try:
        jobs = json.loads(jobs_file.read_text(encoding="utf-8"))
        for job in jobs:
            if job.get("kind") in {"auto_pipeline", "send_real"}:
                add(
                    f"{job.get('kind')} task={job.get('task_name') or job.get('task_id')} "
                    f"started={job.get('started_at')} running={job.get('running')} "
                    f"returncode={job.get('returncode')} pid={job.get('pid')}"
                )
    except (OSError, ValueError, TypeError):
        add("dashboard_jobs.json unavailable")

    add("\nSEND-RELATED JOB LOG LINES (email addresses hidden)")
    log_dir = root / "data" / "job_logs"
    log_files = sorted(log_dir.glob("*.log")) if log_dir.is_dir() else []
    if not log_files:
        add("No job log files found")
    for path in log_files:
        if "auto_pipeline" not in path.name and "send_real" not in path.name:
            continue
        add(f"-- {path.name}; modified {dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone():%Y-%m-%d %H:%M:%S} --")
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                if any(marker in line.lower() for marker in LOG_MARKERS):
                    add(f"{number}: {redact(line.strip())}")

    add("\nPOWER SETTINGS")
    power = command(["pmset", "-g", "custom"])
    add(*(line for line in power.splitlines() if any(
        key in line.lower() for key in ("sleep", "hibern", "powernap", "standby", "womp")
    )))
    add("\nSLEEP / WAKE SYSTEM LOG")
    power_log = command(["pmset", "-g", "log"], timeout=60)
    count = 0
    for line in power_log.splitlines():
        if line[:10] >= str(since) and re.match(
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4} (?:Sleep|Wake|DarkWake)\s", line
        ):
            add(redact(line))
            count += 1
    add(f"Matching sleep/wake log lines: {count}")

    add("\nCURRENT TRADE CRAWL PROCESSES")
    processes = command(["ps", "-axo", "pid=,etime=,stat=,command="])
    for line in processes.splitlines():
        if "trade_outreach.cli" in line and "diagnose_send_gaps" not in line:
            parts = line.strip().split(maxsplit=3)
            if len(parts) == 4:
                mode = "auto-pipeline" if "auto-pipeline" in parts[3] else "send" if " send " in parts[3] else "other"
                add(f"PID={parts[0]} elapsed={parts[1]} status={parts[2]} mode={mode}")
    add("\nEND OF REPORT")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="2026-10-03", help="Local date, YYYY-MM-DD")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    print(report(args.root.resolve(), dt.date.fromisoformat(args.since)), end="")
