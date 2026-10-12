from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_cwd(pid: int) -> Path | None:
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            text=True,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            return Path(line[1:]).resolve()
    return None


def match_task_for_command(command: str, tasks: list[dict]) -> dict | None:
    if "-m trade_outreach.cli auto-pipeline" not in command:
        return None
    if command.lstrip().startswith("caffeinate "):
        return None
    for task in tasks:
        queries_marker = f"--queries-file {task.get('queries_file', '')}"
        countries_marker = f"--countries-file {task.get('countries_file', '')}"
        if queries_marker in command and countries_marker in command:
            return task
    return None


def discover_existing_auto_pipelines(root: Path, tasks: list[dict]) -> list[dict]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,command="],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.+)$", line)
        if not match:
            continue
        pid = int(match.group(1))
        pgid = int(match.group(2))
        command = match.group(3)
        task = match_task_for_command(command, tasks)
        if not task or not process_is_running(pid):
            continue
        cwd = process_cwd(pid)
        if cwd is not None and cwd != root.resolve():
            continue
        found.append(
            {
                "pid": pid,
                "pgid": pgid,
                "task_id": str(task.get("id", "")),
                "task_name": str(task.get("name", "")),
                "command": command,
            }
        )
    return found


def load_job_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8") or "[]")
    except (OSError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def write_job_records(path: Path, jobs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = []
    for job in jobs:
        clean.append({key: value for key, value in job.items() if key != "process"})
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_log_tail(path: str, max_bytes: int = 200_000) -> str:
    if not path:
        return ""
    target = Path(path)
    if not target.exists():
        return ""
    with target.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(size - max_bytes, 0))
        data = handle.read()
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
        return "[Earlier log output omitted.]\n" + text
    return text
