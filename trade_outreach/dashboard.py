from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .background_jobs import (
    discover_existing_auto_pipelines,
    load_job_records,
    process_is_running,
    read_log_tail,
    write_job_records,
)
from .config import get_settings
from .db import connect, init_db
from .discovery_schedule import country_discovery_status
from .llm import generate_text
from .scoring import clean_emails_for_website, is_valid_business_email
from .send_control import global_sent_state
from .templates import draft_body, subject_for


ROOT = Path(__file__).resolve().parents[1]
TASKS_FILE = ROOT / "data" / "tasks.json"
TASK_DIR = ROOT / "data" / "tasks"
JOBS_FILE = ROOT / "data" / "dashboard_jobs.json"
JOB_LOG_DIR = ROOT / "data" / "job_logs"
DEFAULT_TASK_ID = "default"
DEFAULT_COMMAND_TIMEOUT = 600
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
STARTING_JOBS: set[tuple[str, str]] = set()
COMMAND_TIMEOUTS = {
    "crawl_emails": 90000,
    "draft": 90000,
    "discover_preview": 240,
    "discover_import": 240,
    "discover_import_30": 1800,
    "discover_import_90": 5400,
    "discover_more_targets": 5400,
    "discover_agent_targets": 5400,
    "regenerate_unsent": 90000,
    "send_real": 90000,
    "auto_pipeline": 90000,
}
ALLOWED_COMMANDS: dict[str, list[str]] = {
    "stats": ["python3", "-m", "trade_outreach.cli", "stats"],
    "test_llm": ["python3", "-m", "trade_outreach.cli", "test-llm"],
    "test_smtp": ["python3", "-m", "trade_outreach.cli", "test-smtp"],
    "discover_preview": [
        "python3",
        "-m",
        "trade_outreach.cli",
        "discover-batch",
        "--limit-queries",
        "1",
        "--limit-countries",
        "1",
        "--count",
        "3",
    ],
    "discover_import": [
        "python3",
        "-m",
        "trade_outreach.cli",
        "discover-batch",
        "--limit-queries",
        "1",
        "--limit-countries",
        "1",
        "--count",
        "3",
        "--execute",
    ],
    "discover_import_30": [
        "python3",
        "-m",
        "trade_outreach.cli",
        "discover-batch",
        "--limit-queries",
        "3",
        "--limit-countries",
        "3",
        "--count",
        "20",
        "--execute",
    ],
    "discover_import_90": [
        "python3",
        "-m",
        "trade_outreach.cli",
        "discover-batch",
        "--limit-queries",
        "5",
        "--limit-countries",
        "6",
        "--count",
        "20",
        "--execute",
    ],
    "discover_more_targets": [
        "python3",
        "-m",
        "trade_outreach.cli",
        "discover-batch",
        "--offset-queries",
        "5",
        "--limit-queries",
        "8",
        "--limit-countries",
        "12",
        "--count",
        "20",
        "--execute",
    ],
    "discover_agent_targets": [
        "python3",
        "-m",
        "trade_outreach.cli",
        "discover-batch",
        "--limit-queries",
        "3",
        "--scheduled-countries",
        "12",
        "--count",
        "20",
        "--execute",
    ],
    "crawl_emails": [
        "python3",
        "-m",
        "trade_outreach.cli",
        "scrape",
        "--limit",
        "999",
        "--retry-no-email",
        "--fetch-attempts",
        "12",
        "--fetch-timeout",
        "3",
        "--site-max-seconds",
        "45",
        "--workers",
        "4",
    ],
    "score": ["python3", "-m", "trade_outreach.cli", "score", "--rescore"],
    "draft": ["python3", "-m", "trade_outreach.cli", "draft", "--limit", "0", "--no-cache", "--include-personal"],
    "regenerate_unsent": ["python3", "-m", "trade_outreach.cli", "regenerate-unsent-drafts", "--no-cache"],
    "export": ["python3", "-m", "trade_outreach.cli", "export-csv", "data/drafts.csv"],
    "approve_preview": [
        "python3",
        "-m",
        "trade_outreach.cli",
        "approve-batch",
        "--limit",
        "999",
        "--min-score",
        "0",
        "--include-personal",
    ],
    "approve_execute": [
        "python3",
        "-m",
        "trade_outreach.cli",
        "approve-batch",
        "--limit",
        "999",
        "--min-score",
        "0",
        "--include-personal",
        "--execute",
    ],
    "send_dry_run": ["python3", "-m", "trade_outreach.cli", "send", "--limit", "100", "--delay-seconds", "0", "--dry-run"],
    "send_real": [
        "caffeinate",
        "-dimsu",
        "python3",
        "-m",
        "trade_outreach.cli",
        "send",
        "--limit",
        "100",
        "--delay-seconds",
        "800",
    ],
    "auto_pipeline": [
        "caffeinate",
        "-dimsu",
        "python3",
        "-m",
        "trade_outreach.cli",
        "auto-pipeline",
        "--cycles",
        "0",
        "--sleep-seconds",
        "0",
        "--idle-sleep-seconds",
        "300",
        "--discovery-interval-seconds",
        "900",
        "--processing-interval-seconds",
        "60",
        "--limit-queries",
        "3",
        "--limit-countries",
        "3",
        "--scheduled-countries",
        "3",
        "--discover-count",
        "20",
        "--scrape-limit",
        "999",
        "--include-personal",
        "--send-limit",
        "5",
        "--delay-seconds",
        "800",
    ],
}
COUNTRY_PRESETS: dict[str, list[str]] = {
    "Africa": [
        "Algeria",
        "Angola",
        "Benin",
        "Botswana",
        "Burkina Faso",
        "Burundi",
        "Cabo Verde",
        "Cameroon",
        "Central African Republic",
        "Chad",
        "Comoros",
        "Cote d'Ivoire",
        "Democratic Republic of the Congo",
        "Djibouti",
        "Egypt",
        "Equatorial Guinea",
        "Eritrea",
        "Eswatini",
        "Ethiopia",
        "Gabon",
        "Gambia",
        "Ghana",
        "Guinea",
        "Guinea-Bissau",
        "Kenya",
        "Lesotho",
        "Liberia",
        "Libya",
        "Madagascar",
        "Malawi",
        "Mali",
        "Mauritania",
        "Mauritius",
        "Morocco",
        "Mozambique",
        "Namibia",
        "Niger",
        "Nigeria",
        "Republic of the Congo",
        "Rwanda",
        "Sao Tome and Principe",
        "Senegal",
        "Seychelles",
        "Sierra Leone",
        "Somalia",
        "South Africa",
        "South Sudan",
        "Sudan",
        "Tanzania",
        "Togo",
        "Tunisia",
        "Uganda",
        "Zambia",
        "Zimbabwe",
    ],
    "Asia": [
        "Afghanistan",
        "Armenia",
        "Azerbaijan",
        "Bahrain",
        "Bangladesh",
        "Bhutan",
        "Brunei",
        "Cambodia",
        "Cyprus",
        "Georgia",
        "India",
        "Indonesia",
        "Iran",
        "Iraq",
        "Israel",
        "Japan",
        "Jordan",
        "Kazakhstan",
        "Kuwait",
        "Kyrgyzstan",
        "Laos",
        "Lebanon",
        "Malaysia",
        "Maldives",
        "Mongolia",
        "Myanmar",
        "Nepal",
        "North Korea",
        "Oman",
        "Pakistan",
        "Palestine",
        "Philippines",
        "Qatar",
        "Saudi Arabia",
        "Singapore",
        "South Korea",
        "Sri Lanka",
        "Syria",
        "Taiwan",
        "Tajikistan",
        "Thailand",
        "Timor-Leste",
        "Turkey",
        "Turkmenistan",
        "United Arab Emirates",
        "Uzbekistan",
        "Vietnam",
        "Yemen",
    ],
    "Europe": [
        "Albania",
        "Andorra",
        "Austria",
        "Belarus",
        "Belgium",
        "Bosnia and Herzegovina",
        "Bulgaria",
        "Croatia",
        "Cyprus",
        "Czech Republic",
        "Denmark",
        "Estonia",
        "Finland",
        "France",
        "Germany",
        "Greece",
        "Hungary",
        "Iceland",
        "Ireland",
        "Italy",
        "Kosovo",
        "Latvia",
        "Liechtenstein",
        "Lithuania",
        "Luxembourg",
        "Malta",
        "Moldova",
        "Monaco",
        "Montenegro",
        "Netherlands",
        "North Macedonia",
        "Norway",
        "Poland",
        "Portugal",
        "Romania",
        "Russia",
        "San Marino",
        "Serbia",
        "Slovakia",
        "Slovenia",
        "Spain",
        "Sweden",
        "Switzerland",
        "Turkey",
        "Ukraine",
        "United Kingdom",
        "Vatican City",
    ],
    "North America": [
        "Antigua and Barbuda",
        "Bahamas",
        "Barbados",
        "Belize",
        "Canada",
        "Costa Rica",
        "Cuba",
        "Dominica",
        "Dominican Republic",
        "El Salvador",
        "Grenada",
        "Guatemala",
        "Haiti",
        "Honduras",
        "Jamaica",
        "Mexico",
        "Nicaragua",
        "Panama",
        "Saint Kitts and Nevis",
        "Saint Lucia",
        "Saint Vincent and the Grenadines",
        "Trinidad and Tobago",
        "United States",
    ],
    "Oceania": [
        "Australia",
        "Fiji",
        "Kiribati",
        "Marshall Islands",
        "Micronesia",
        "Nauru",
        "New Zealand",
        "Palau",
        "Papua New Guinea",
        "Samoa",
        "Solomon Islands",
        "Tonga",
        "Tuvalu",
        "Vanuatu",
    ],
    "South America": [
        "Argentina",
        "Bolivia",
        "Brazil",
        "Chile",
        "Colombia",
        "Ecuador",
        "Guyana",
        "Paraguay",
        "Peru",
        "Suriname",
        "Uruguay",
        "Venezuela",
    ],
}


def task_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "task"


def ensure_tasks() -> list[dict]:
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if TASKS_FILE.exists():
        tasks = json.loads(TASKS_FILE.read_text(encoding="utf-8") or "[]")
    else:
        settings = get_settings()
        default_task_dir = TASK_DIR / DEFAULT_TASK_ID
        default_task_dir.mkdir(parents=True, exist_ok=True)
        default_queries = default_task_dir / "queries.txt"
        default_countries = default_task_dir / "countries.txt"
        if not default_queries.exists():
            shutil.copyfile(ROOT / "examples/discovery_queries.txt", default_queries)
        if not default_countries.exists():
            shutil.copyfile(ROOT / "examples/countries.txt", default_countries)
        tasks = [
            {
                "id": DEFAULT_TASK_ID,
                "name": "Steel Pipe",
                "db_path": "data/outreach.sqlite3",
                "queries_file": str(default_queries.relative_to(ROOT)),
                "countries_file": str(default_countries.relative_to(ROOT)),
                "profile": {
                    "target_region": settings.target_region,
                    "company_name": settings.company_name,
                    "company_address": settings.company_address,
                    "business_summary": settings.company_business_summary,
                    "items": default_profile_items(settings),
                },
            }
        ]
        write_tasks(tasks)
    return tasks


def default_profile_items(settings) -> list[dict]:
    items = []
    if settings.company_facebook_url:
        items.append({"label": "Facebook", "value": settings.company_facebook_url})
    if settings.company_extra_info:
        for line in settings.company_extra_info.splitlines():
            if ":" in line:
                label, value = line.split(":", 1)
                items.append({"label": label.strip(), "value": value.strip()})
    return items


def write_tasks(tasks: list[dict]) -> None:
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TASKS_FILE.write_text(json.dumps(tasks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_tasks() -> list[dict]:
    return ensure_tasks()


def get_task(task_id: str | None) -> dict:
    tasks = ensure_tasks()
    if task_id:
        for task in tasks:
            if task["id"] == task_id:
                return task
    return tasks[0]


def task_path(task: dict, key: str) -> Path:
    return ROOT / task[key]


def task_profile(task: dict) -> dict:
    profile = task.setdefault("profile", {})
    profile.setdefault("target_region", "")
    profile.setdefault("company_name", "")
    profile.setdefault("company_address", "")
    profile.setdefault("business_summary", "")
    profile.setdefault("items", [])
    return profile


def profile_extra_info(profile: dict) -> str:
    lines = []
    dedicated_labels = {"facebook", "facebook link", "fb", "fb link"}
    for item in profile.get("items", []):
        label = clean_single_line(item.get("label", ""))
        value = clean_single_line(item.get("value", ""))
        if label.lower() in dedicated_labels:
            continue
        if label and value:
            lines.append(f"{label}: {value}")
        elif value:
            lines.append(value)
    return "\n".join(lines)


def blank_profile() -> dict:
    return {
        "target_region": "",
        "company_name": "",
        "company_address": "",
        "business_summary": "",
        "items": [],
    }


def profile_prompt_context(task: dict) -> str:
    profile = task_profile(task)
    lines = [
        "Current task company profile for email generation:",
        f"- Task: {task.get('name', '')}",
        f"- Company location/region: {profile.get('target_region', '')}",
        f"- Company name: {profile.get('company_name', '')}",
        f"- Company mailing address: {profile.get('company_address', '')}",
        f"- Business summary: {profile.get('business_summary', '')}",
    ]
    for item in profile.get("items", []):
        label = clean_single_line(item.get("label", ""))
        value = clean_single_line(item.get("value", ""))
        if label or value:
            lines.append(f"- {label or 'Info'}: {value}")
    return "\n".join(lines)


def task_settings(task: dict):
    settings = get_settings()
    profile = task_profile(task)
    facebook = ""
    for item in profile.get("items", []):
        if clean_single_line(item.get("label", "")).lower() in {"facebook", "facebook link"}:
            facebook = clean_single_line(item.get("value", ""))
            break
    return replace(
        settings,
        db_path=str(task_path(task, "db_path")),
        target_region=profile.get("target_region", settings.target_region),
        company_name=profile.get("company_name", settings.company_name),
        company_address=profile.get("company_address", settings.company_address),
        company_business_summary=profile.get("business_summary", settings.company_business_summary),
        company_facebook_url=facebook,
        company_extra_info=profile_extra_info(profile),
    )


def task_env(task: dict) -> dict[str, str]:
    settings = task_settings(task)
    env = {
        "OUTREACH_DB": settings.db_path,
        "TARGET_REGION": settings.target_region,
        "COMPANY_NAME": settings.company_name,
        "COMPANY_ADDRESS": settings.company_address,
        "COMPANY_BUSINESS_SUMMARY": settings.company_business_summary,
        "COMPANY_FACEBOOK_URL": settings.company_facebook_url,
        "COMPANY_EXTRA_INFO": settings.company_extra_info,
    }
    return env


def create_task(name: str, clone_from: str = "") -> dict:
    tasks = ensure_tasks()
    clean_name = clean_single_line(name) or "New Task"
    task_id = f"{task_slug(clean_name)}-{uuid.uuid4().hex[:6]}"
    base_dir = TASK_DIR / task_id
    base_dir.mkdir(parents=True, exist_ok=True)
    source_task = get_task(clone_from) if clone_from else None
    task = {
        "id": task_id,
        "name": clean_name,
        "db_path": str((base_dir / "outreach.sqlite3").relative_to(ROOT)),
        "queries_file": str((base_dir / "queries.txt").relative_to(ROOT)),
        "countries_file": str((base_dir / "countries.txt").relative_to(ROOT)),
        "profile": json.loads(json.dumps(task_profile(source_task), ensure_ascii=False)) if source_task else blank_profile(),
    }
    if source_task:
        shutil.copyfile(task_path(source_task, "queries_file"), task_path(task, "queries_file"))
        shutil.copyfile(task_path(source_task, "countries_file"), task_path(task, "countries_file"))
    else:
        task_path(task, "queries_file").write_text("", encoding="utf-8")
        task_path(task, "countries_file").write_text("", encoding="utf-8")
    init_db(connect(str(task_path(task, "db_path"))))
    tasks.append(task)
    write_tasks(tasks)
    return task


def rename_task(task_id: str, name: str) -> dict:
    tasks = ensure_tasks()
    clean_name = clean_single_line(name)
    if not clean_name:
        return {"ok": False, "output": "Task name is required."}
    for task in tasks:
        if task["id"] == task_id:
            task["name"] = clean_name
            write_tasks(tasks)
            return {"ok": True, "output": f"Renamed task to {clean_name}."}
    return {"ok": False, "output": "Task not found."}


def delete_task(task_id: str) -> dict:
    tasks = ensure_tasks()
    if task_id == DEFAULT_TASK_ID:
        return {"ok": False, "output": "The default task cannot be deleted."}
    if get_active_job("auto_pipeline", task_id) or get_active_job("send_real", task_id):
        return {"ok": False, "output": "Stop this task's running Auto Pipeline or send job before deleting it."}
    next_tasks = [task for task in tasks if task["id"] != task_id]
    if len(next_tasks) == len(tasks):
        return {"ok": False, "output": "Task not found."}
    write_tasks(next_tasks)
    return {"ok": True, "output": "Deleted task from the task list. Data files were left on disk for safety."}


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(TASKS_HTML)
            return
        if parsed.path == "/dashboard":
            self.send_html(INDEX_HTML)
            return
        if parsed.path == "/api/tasks":
            self.send_json({"tasks": task_summaries()})
            return
        if parsed.path == "/api/state":
            task = get_task(parse_qs(parsed.query).get("task", [""])[0])
            self.send_json(build_state(task))
            return
        if parsed.path == "/api/job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            self.send_json(get_job(job_id))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/task-create":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            task = create_task(str(payload.get("name", "")), str(payload.get("clone_from", "")))
            self.send_json({"ok": True, "task": task, "output": f"Created task {task['name']}."})
            return
        if parsed.path == "/api/task-rename":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(rename_task(str(payload.get("task_id", "")), str(payload.get("name", ""))))
            return
        if parsed.path == "/api/task-delete":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(delete_task(str(payload.get("task_id", ""))))
            return
        if parsed.path == "/api/tasks-auto-start":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(start_auto_pipelines(payload.get("task_ids", [])))
            return
        if parsed.path == "/api/tasks-auto-stop":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(stop_auto_pipelines(payload.get("task_ids", [])))
            return
        if parsed.path == "/api/run":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            command_id = str(payload.get("command", ""))
            self.send_json(run_command(command_id, get_task(str(payload.get("task_id", "")))))
            return
        if parsed.path == "/api/job-stop":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(stop_job(str(payload.get("job_id", ""))))
            return
        if parsed.path == "/api/save-settings":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(save_public_settings(payload, get_task(str(payload.get("task_id", "")))))
            return
        if parsed.path == "/api/list-item":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(update_list_item(payload, get_task(str(payload.get("task_id", "")))))
            return
        if parsed.path == "/api/countries-preset":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(add_country_preset(payload, get_task(str(payload.get("task_id", "")))))
            return
        if parsed.path == "/api/generate-queries":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(generate_target_queries(get_task(str(payload.get("task_id", "")))))
            return
        if parsed.path == "/api/approve":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            draft_id = int(payload.get("draft_id", 0))
            self.send_json(run_custom(["python3", "-m", "trade_outreach.cli", "approve-draft", str(draft_id)], task=get_task(str(payload.get("task_id", "")))))
            return
        if parsed.path == "/api/unapprove":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            draft_id = int(payload.get("draft_id", 0))
            self.send_json(run_custom(["python3", "-m", "trade_outreach.cli", "unapprove-draft", str(draft_id)], task=get_task(str(payload.get("task_id", "")))))
            return
        if parsed.path == "/api/regenerate-draft":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            draft_id = int(payload.get("draft_id", 0))
            self.send_json(run_custom(["python3", "-m", "trade_outreach.cli", "regenerate-draft", str(draft_id), "--no-cache"], task=get_task(str(payload.get("task_id", "")))))
            return
        if parsed.path == "/api/send-test":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            draft_id = int(payload.get("draft_id", 0))
            recipient = str(payload.get("recipient", "")).strip()
            dry_run = bool(payload.get("dry_run", True))
            if not recipient or "@" not in recipient:
                self.send_json({"ok": False, "output": "Recipient email is required."}, status=400)
                return
            task = get_task(str(payload.get("task_id", "")))
            cmd = ["python3", "-m", "trade_outreach.cli", "send-test", str(draft_id), recipient]
            if dry_run:
                cmd.append("--dry-run")
            self.send_json(run_custom(cmd, task=task))
            return
        if parsed.path == "/api/send-draft":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            draft_id = int(payload.get("draft_id", 0))
            self.send_json(run_custom(["python3", "-m", "trade_outreach.cli", "send-draft", str(draft_id)], timeout=1800, task=get_task(str(payload.get("task_id", "")))))
            return
        if parsed.path == "/api/rescrape-url":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            url = str(payload.get("url", "")).strip()
            if not url:
                self.send_json({"ok": False, "output": "URL is required."}, status=400)
                return
            self.send_json(run_custom(["python3", "-m", "trade_outreach.cli", "rescrape-url", url], timeout=900, task=get_task(str(payload.get("task_id", "")))))
            return
        if parsed.path == "/api/manual-email-draft":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            email = str(payload.get("email", "")).strip()
            company = str(payload.get("company", "")).strip()
            if not email or "@" not in email:
                self.send_json({"ok": False, "output": "Valid email is required."}, status=400)
                return
            task = get_task(str(payload.get("task_id", "")))
            cmd = ["python3", "-m", "trade_outreach.cli", "manual-email-draft", email]
            if company:
                cmd.extend(["--company", company])
            self.send_json(run_custom(cmd, timeout=300, task=task))
            return
        if parsed.path == "/api/lead-email":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            lead_id = int(payload.get("lead_id", 0))
            email = str(payload.get("email", "")).strip().lower()
            self.send_json(add_email_to_lead(lead_id, email, get_task(str(payload.get("task_id", "")))))
            return
        if parsed.path == "/api/profile-item":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(update_profile_item(payload, get_task(str(payload.get("task_id", "")))))
            return
        if parsed.path == "/api/import-leads":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            path = str(payload.get("path", "")).strip()
            if not path:
                self.send_json({"ok": False, "output": "CSV path is required."}, status=400)
                return
            self.send_json(run_custom(["python3", "-m", "trade_outreach.cli", "import-leads-csv", path], timeout=600, task=get_task(str(payload.get("task_id", "")))))
            return
        self.send_error(404)

    def log_message(self, format: str, *args) -> None:
        return

    def send_html(self, content: str) -> None:
        data = content.encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return

    def send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return


def build_state(task: dict) -> dict:
    settings = task_settings(task)
    conn = connect(settings.db_path)
    init_db(conn)
    stats = {
        "total_leads": conn.execute("SELECT COUNT(*) AS c FROM leads").fetchone()["c"],
        "leads_with_emails": conn.execute("SELECT COUNT(*) AS c FROM leads WHERE emails IS NOT NULL AND emails != '[]'").fetchone()["c"],
        "drafts": conn.execute("SELECT COUNT(*) AS c FROM email_drafts").fetchone()["c"],
        "approved": conn.execute("SELECT COUNT(*) AS c FROM email_drafts WHERE approved = 1").fetchone()["c"],
        "approved_unsent": conn.execute("SELECT COUNT(*) AS c FROM email_drafts WHERE approved = 1 AND sent_at IS NULL").fetchone()["c"],
        "pending_unsent": conn.execute("SELECT COUNT(*) AS c FROM email_drafts WHERE approved = 0 AND sent_at IS NULL").fetchone()["c"],
        "sent": conn.execute("SELECT COUNT(*) AS c FROM email_drafts WHERE sent_at IS NOT NULL").fetchone()["c"],
        "sent_today": conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM email_drafts
            WHERE sent_at IS NOT NULL
              AND date(sent_at, 'localtime') = date('now', 'localtime')
            """
        ).fetchone()["c"],
    }
    stats.update(build_metric_stats(conn))
    global_sent_today, _ = global_sent_state(settings.db_path)
    stats["task_sent_today"] = stats["sent_today"]
    stats["global_sent_today"] = global_sent_today
    stats["daily_limit"] = settings.send_daily_limit
    stats["remaining_today"] = max(settings.send_daily_limit - global_sent_today, 0)
    stats["by_status"] = [dict(row) for row in conn.execute("SELECT status, COUNT(*) AS count FROM leads GROUP BY status ORDER BY status")]
    leads = [lead_to_dict(row) for row in conn.execute(
        """
        SELECT id, company_name, website, source, country, status, score, emails, phones, llm_note, updated_at
        FROM leads ORDER BY score DESC, id DESC LIMIT 200
        """
    )]
    drafts = [dict(row) for row in conn.execute(
        """
        SELECT d.id, d.recipient, d.subject, d.body, d.approved, d.sent_at, d.created_at,
               l.company_name, l.website, l.score
        FROM email_drafts d JOIN leads l ON l.id = d.lead_id
        ORDER BY d.id DESC LIMIT 200
        """
    )]
    discovery_runs = [dict(row) for row in conn.execute(
        """
        SELECT country, query, base_query, city, round_no, phase,
               result_count, unique_website_count, new_website_count, error, searched_at
        FROM discovery_runs
        ORDER BY id DESC LIMIT 200
        """
    )]
    config = {
        "task_id": task["id"],
        "task_name": task["name"],
        "target_region": settings.target_region,
        "company_name": settings.company_name,
        "company_address": settings.company_address,
        "company_business_summary": settings.company_business_summary,
        "company_items": task_profile(task).get("items", []),
        "company_facebook_url": settings.company_facebook_url,
        "google_places_key": mask(settings.google_places_api_key),
        "google_region": settings.google_places_region_code,
        "llm": f"{settings.llm_provider} / {settings.deepseek_model}",
        "mail_from": settings.mail_from,
        "mail_bcc": settings.mail_bcc,
        "facebook": settings.company_facebook_url,
        "smtp": f"{settings.smtp_host}:{settings.smtp_port} ({settings.smtp_security})",
        "send_delay_seconds": settings.send_delay_seconds,
        "send_daily_limit": settings.send_daily_limit,
    }
    country_items = read_list_items(task["countries_file"])
    country_discovery = country_discovery_status(conn, settings, country_items)
    return {
        "stats": stats,
        "leads": leads,
        "drafts": drafts,
        "discovery_runs": discovery_runs,
        "config": config,
        "prompt": profile_prompt_context(task) + "\n\n" + read_text("trade_outreach/prompts.py"),
        "template": read_text("trade_outreach/templates.py"),
        "queries": read_text(task["queries_file"]),
        "countries": read_text(task["countries_file"]),
        "query_items": read_list_items(task["queries_file"]),
        "country_items": country_items,
        "country_groups": group_countries_by_continent(country_items),
        "country_coverage": build_country_coverage(conn, country_items),
        "country_discovery": country_discovery,
        "country_presets": {name: len(countries) for name, countries in COUNTRY_PRESETS.items()},
        "active_auto_pipeline_job": get_active_job("auto_pipeline", task["id"]),
        "active_regenerate_job": get_active_job("regenerate_unsent", task["id"]),
    }


def continent_display_order() -> list[str]:
    # Prefer Europe for transcontinental countries such as Cyprus and Turkey in this B2B task UI.
    return ["Europe", "Asia", "Africa", "North America", "South America", "Oceania"]


def group_countries_by_continent(countries: list[str]) -> list[dict]:
    remaining = {country.lower(): country for country in countries}
    groups = []
    for continent in continent_display_order():
        preset_countries = COUNTRY_PRESETS.get(continent, [])
        items = []
        for preset_country in preset_countries:
            country = remaining.pop(preset_country.lower(), None)
            if country:
                items.append(country)
        if items:
            groups.append({"continent": continent, "countries": items})
    if remaining:
        groups.append({"continent": "Custom / Other", "countries": list(remaining.values())})
    return groups


def country_continent(country: str) -> str:
    clean = country.lower().strip()
    for continent in continent_display_order():
        if clean in {item.lower() for item in COUNTRY_PRESETS.get(continent, [])}:
            return continent
    return "Custom / Other"

def build_country_coverage(conn: sqlite3.Connection, countries: list[str]) -> dict[str, int]:
    counts = {
        (row["country"] or "").strip().lower(): row["count"]
        for row in conn.execute(
            """
            SELECT country, COUNT(*) AS count
            FROM leads
            WHERE country IS NOT NULL AND country != ''
            GROUP BY country
            """
        )
    }
    return {country: counts.get(country.lower(), 0) for country in countries}


def build_metric_stats(conn: sqlite3.Connection) -> dict:
    lead_rows = conn.execute("SELECT status, emails FROM leads").fetchall()
    total_leads = len(lead_rows)
    scraped_statuses = {"scraped", "no_email", "scored", "drafted", "sent"}
    scraped_leads = sum(1 for row in lead_rows if row["status"] in scraped_statuses)
    failed_scrapes = sum(1 for row in lead_rows if row["status"] == "scrape_failed")
    no_email = sum(1 for row in lead_rows if row["status"] == "no_email")
    leads_with_valid_email = 0
    total_valid_emails = 0
    total_raw_emails = 0
    for row in lead_rows:
        emails = parse_json_list(row["emails"])
        total_raw_emails += len(emails)
        valid = [email for email in emails if is_valid_business_email(email)]
        total_valid_emails += len(valid)
        if valid:
            leads_with_valid_email += 1

    draft_rows = conn.execute("SELECT lead_id, approved, sent_at, recipient FROM email_drafts").fetchall()
    total_drafts = len(draft_rows)
    drafted_leads = len({row["lead_id"] for row in draft_rows})
    approved = sum(1 for row in draft_rows if row["approved"])
    sent = sum(1 for row in draft_rows if row["sent_at"])
    unsent = sum(1 for row in draft_rows if not row["sent_at"])
    invalid_unsent = sum(1 for row in draft_rows if not row["sent_at"] and not is_valid_business_email(row["recipient"]))
    invalid_approved = sum(
        1
        for row in draft_rows
        if row["approved"] and not row["sent_at"] and not is_valid_business_email(row["recipient"])
    )
    bounce_count = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM suppression_list
        WHERE lower(reason) LIKE '%bounce%'
           OR lower(reason) LIKE '%退信%'
        """
    ).fetchone()["c"]
    return {
        "scraped_leads": scraped_leads,
        "failed_scrapes": failed_scrapes,
        "no_email": no_email,
        "leads_with_valid_email": leads_with_valid_email,
        "total_raw_emails": total_raw_emails,
        "total_valid_emails": total_valid_emails,
        "email_crawl_rate": percent(leads_with_valid_email, total_leads),
        "email_find_rate_after_scrape": percent(leads_with_valid_email, scraped_leads),
        "scrape_failure_rate": percent(failed_scrapes, total_leads),
        "drafted_leads": drafted_leads,
        "draft_rate": percent(drafted_leads, total_leads),
        "approval_rate": percent(approved, total_drafts),
        "send_rate": percent(sent, total_drafts),
        "approved_send_rate": percent(sent, approved),
        "unsent_drafts": unsent,
        "invalid_unsent_drafts": invalid_unsent,
        "invalid_approved_drafts": invalid_approved,
        "bounce_count": bounce_count,
        "bounce_rate": percent(bounce_count, sent) if bounce_count else None,
    }


def percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator * 100 / denominator, 1)


def lead_to_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["emails"] = parse_json_list(item.get("emails"))
    item["phones"] = parse_json_list(item.get("phones"))
    if not item.get("country"):
        item["country"] = infer_country_from_source(item.get("source", ""))
    return item


def infer_country_from_source(source: str) -> str:
    source_lower = (source or "").lower()
    if not source_lower:
        return ""
    for country in read_text("examples/countries.txt").splitlines():
        clean = country.strip()
        if clean and not clean.startswith("#") and clean.lower() in source_lower:
            return clean
    return ""


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def add_email_to_lead(lead_id: int, email: str, task: dict) -> dict:
    if not lead_id:
        return {"ok": False, "output": "Lead id is required."}
    if "@" not in email:
        return {"ok": False, "output": "Valid email is required."}
    settings = task_settings(task)
    conn = connect(settings.db_path)
    init_db(conn)
    row = conn.execute(
        "SELECT id, company_name, website, status, emails FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    if not row:
        return {"ok": False, "output": f"Lead #{lead_id} not found."}
    existing = parse_json_list(row["emails"])
    merged = clean_emails_for_website(existing + [email], row["website"])
    if email not in merged:
        return {"ok": False, "output": f"Email was rejected by validation/filtering: {email}"}
    next_status = row["status"]
    if next_status in {"new", "no_email", "scrape_failed"}:
        next_status = "scraped"
    conn.execute(
        """
        UPDATE leads
        SET emails = ?, status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (json.dumps(merged), next_status, lead_id),
    )
    existing_draft = conn.execute(
        """
        SELECT id FROM email_drafts
        WHERE lead_id = ? AND lower(recipient) = lower(?) AND sent_at IS NULL
        """,
        (lead_id, email),
    ).fetchone()
    draft_msg = ""
    if not existing_draft:
        opening = f"I am reaching out to introduce {settings.company_name} as a potential supplier for your business."
        conn.execute(
            """
            INSERT INTO email_drafts (lead_id, recipient, subject, body)
            VALUES (?, ?, ?, ?)
            """,
            (
                lead_id,
                email,
                subject_for(row["company_name"]),
                draft_body(settings, row["company_name"], opening, settings.mail_reply_to),
            ),
        )
        conn.execute("UPDATE leads SET status = 'drafted', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (lead_id,))
        draft_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        draft_msg = f" Created draft #{draft_id}."
    conn.commit()
    return {"ok": True, "output": f"Added {email} to lead #{lead_id}. Total emails: {len(merged)}.{draft_msg}"}


def save_public_settings(payload: dict, task: dict) -> dict:
    tasks = ensure_tasks()
    profile = task_profile(task)
    profile["target_region"] = clean_single_line(payload.get("target_region", ""))
    profile["company_name"] = clean_single_line(payload.get("company_name", ""))
    profile["company_address"] = clean_single_line(payload.get("company_address", ""))
    profile["business_summary"] = clean_single_line(payload.get("business_summary", ""))
    for item in tasks:
        if item["id"] == task["id"]:
            item["profile"] = profile
            break
    write_tasks(tasks)
    return {"ok": True, "output": "Saved company profile for this task. New drafts will use these values."}


def clean_single_line(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def write_env_values(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    next_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            next_lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            next_lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            next_lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            next_lines.append(f"{key}={value}")
    path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")


def update_list_item(payload: dict, task: dict) -> dict:
    list_name = str(payload.get("list", "")).strip()
    action = str(payload.get("action", "")).strip()
    value = clean_single_line(payload.get("value", ""))
    if list_name == "queries":
        path_name = task["queries_file"]
    elif list_name == "countries":
        path_name = task["countries_file"]
    else:
        return {"ok": False, "output": "Unknown list."}
    if action not in {"add", "delete"}:
        return {"ok": False, "output": "Unknown list action."}
    if not value:
        return {"ok": False, "output": "Value is required."}

    path = ROOT / path_name
    items = read_list_items(path_name)
    if action == "add":
        if value.lower() not in {item.lower() for item in items}:
            items.append(value)
            write_list_items(path, items)
        return {"ok": True, "output": f"Added {value} to {list_name}."}

    next_items = [item for item in items if item.lower() != value.lower()]
    if len(next_items) == len(items):
        return {"ok": False, "output": f"{value} was not found in {list_name}."}
    write_list_items(path, next_items)
    return {"ok": True, "output": f"Deleted {value} from {list_name}."}


def add_country_preset(payload: dict, task: dict) -> dict:
    preset_name = clean_single_line(payload.get("preset", ""))
    countries = COUNTRY_PRESETS.get(preset_name)
    if not countries:
        return {"ok": False, "output": "Unknown country preset."}

    path = task_path(task, "countries_file")
    items = read_list_items(task["countries_file"])
    existing = {item.lower() for item in items}
    additions = [country for country in countries if country.lower() not in existing]
    if additions:
        write_list_items(path, items + additions)
    skipped = len(countries) - len(additions)
    return {
        "ok": True,
        "output": (
            f"Imported {len(additions)} countries from {preset_name}. "
            f"Skipped {skipped} already in this task's countries list."
        ),
    }


COUNTRY_LANGUAGE_HINTS = {
    "Germany": "German",
    "Austria": "German",
    "Switzerland": "German, French, Italian",
    "France": "French",
    "Belgium": "Dutch, French",
    "Netherlands": "Dutch",
    "Spain": "Spanish",
    "Italy": "Italian",
    "Portugal": "Portuguese",
    "Poland": "Polish",
    "Czech Republic": "Czech",
    "Romania": "Romanian",
    "Sweden": "Swedish",
    "Denmark": "Danish",
    "Finland": "Finnish",
    "Norway": "Norwegian",
    "United Kingdom": "English",
    "Ireland": "English",
    "Turkey": "Turkish",
    "Greece": "Greek",
    "Hungary": "Hungarian",
    "Slovakia": "Slovak",
    "Slovenia": "Slovenian",
    "Bulgaria": "Bulgarian",
    "Croatia": "Croatian",
    "Serbia": "Serbian",
    "Ukraine": "Ukrainian",
    "Lithuania": "Lithuanian",
    "Latvia": "Latvian",
    "Estonia": "Estonian",
}


def generate_target_queries(task: dict) -> dict:
    settings = task_settings(task)
    countries = read_list_items(task["countries_file"])
    existing_queries = read_list_items(task["queries_file"])
    profile = task_profile(task)
    language_lines = [
        f"- {country}: {COUNTRY_LANGUAGE_HINTS.get(country, 'English plus local mainstream business language if known')}"
        for country in countries[:60]
    ]
    prompt = f"""
Generate B2B lead discovery search queries for a local outreach tool.

Task name: {task.get("name", "")}
Business/product description: {profile.get("business_summary", "")}
Existing queries:
{json.dumps(existing_queries[:80], ensure_ascii=False, indent=2)}

Countries and language hints:
{chr(10).join(language_lines)}

Return only JSON with this shape:
{{
  "global_queries": ["..."],
  "country_queries": [{{"country": "Germany", "query": "..."}}]
}}

Rules:
- Generate search phrases for finding target customers/buyers/distributors, not email text.
- Use this structure:
  1. broad product/supplier queries,
  2. target customer type queries such as distributor, stockholder, importer, wholesaler, service center, dealer,
  3. product/application niche queries.
- global_queries must be English and reusable across all countries.
- country_queries must use the country's mainstream local business language where useful.
- Do not include country names inside query text; country is appended by the crawler.
- Keep each query 2 to 6 words.
- Avoid overly narrow brand names, city names, and company names.
- Avoid duplicates and near duplicates.
- Limit global_queries to 18.
- Limit country_queries to 1 per country.
- Keep the JSON compact, with no explanations.
"""
    try:
        raw = generate_text(
            settings,
            prompt,
            max_tokens=5000,
            system_prompt="You generate concise B2B search queries and return strict JSON only.",
            json_mode=True,
        )
        generated = parse_generated_queries(raw)
    except Exception as exc:
        return {
            "ok": False,
            "output": (
                f"Query generation failed: {exc}\n\n"
                "Tip: try again. The generator now asks for a shorter JSON response; repeated failures usually mean the LLM returned malformed JSON."
            ),
        }

    additions: list[str] = []
    additions.extend(generated["global_queries"])
    known_countries = {country.lower(): country for country in countries}
    for item in generated["country_queries"]:
        country = known_countries.get(item["country"].lower())
        query = item["query"]
        if country and query:
            additions.append(f"{country} :: {query}")

    path = task_path(task, "queries_file")
    before = len(existing_queries)
    write_list_items(path, existing_queries + additions)
    after_items = read_list_items(task["queries_file"])
    added = max(len(after_items) - before, 0)
    preview = "\n".join(after_items[before : before + 30])
    return {
        "ok": True,
        "output": f"Generated {added} new query item(s). Existing duplicates were skipped.\n\n{preview}",
    }


def parse_generated_queries(raw: str) -> dict[str, list]:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    payload = json.loads(text)
    global_queries = sanitize_query_list(payload.get("global_queries", []), limit=40)
    country_queries = []
    for item in payload.get("country_queries", []):
        if not isinstance(item, dict):
            continue
        country = clean_single_line(item.get("country", ""))
        query = sanitize_query(item.get("query", ""))
        if country and query:
            country_queries.append({"country": country, "query": query})
    return {"global_queries": global_queries, "country_queries": country_queries[:160]}


def sanitize_query_list(values: object, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    queries = []
    for value in values:
        query = sanitize_query(value)
        if query:
            queries.append(query)
    return queries[:limit]


def sanitize_query(value: object) -> str:
    query = clean_single_line(value)
    query = query.strip("\"'`.,;:")
    if not query or len(query) > 80:
        return ""
    if any(token in query.lower() for token in {"@", "http://", "https://"}):
        return ""
    return query


def update_profile_item(payload: dict, task: dict) -> dict:
    action = str(payload.get("action", "")).strip()
    label = clean_single_line(payload.get("label", ""))
    value = clean_single_line(payload.get("value", ""))
    index = int(payload.get("index", -1))
    if action not in {"add", "delete"}:
        return {"ok": False, "output": "Unknown profile item action."}
    tasks = ensure_tasks()
    profile = task_profile(task)
    items = profile.setdefault("items", [])
    if action == "add":
        if not value:
            return {"ok": False, "output": "Item value is required."}
        items.append({"label": label or "Info", "value": value})
        output = f"Added profile item {label or 'Info'}."
    else:
        if index < 0 or index >= len(items):
            return {"ok": False, "output": "Profile item not found."}
        removed = items.pop(index)
        output = f"Deleted profile item {removed.get('label', 'Info')}."
    for item in tasks:
        if item["id"] == task["id"]:
            item["profile"] = profile
            break
    write_tasks(tasks)
    return {"ok": True, "output": output}


def read_list_items(path: str) -> list[str]:
    return [line.strip() for line in read_text(path).splitlines() if line.strip() and not line.strip().startswith("#")]


def write_list_items(path: Path, items: list[str]) -> None:
    clean_items = []
    seen = set()
    for item in items:
        clean = clean_single_line(item)
        if clean and clean.lower() not in seen:
            clean_items.append(clean)
            seen.add(clean.lower())
    path.write_text("\n".join(clean_items).rstrip() + "\n", encoding="utf-8")


def read_text(path: str) -> str:
    target = ROOT / path
    if not target.exists():
        return ""
    return target.read_text(encoding="utf-8")


def mask(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 10:
        return "***"
    return value[:4] + "..." + value[-4:]


def task_command(command_id: str, task: dict) -> list[str] | None:
    commands = json.loads(json.dumps(ALLOWED_COMMANDS))
    for key in {
        "discover_preview",
        "discover_import",
        "discover_import_30",
        "discover_import_90",
        "discover_more_targets",
        "discover_agent_targets",
        "auto_pipeline",
    }:
        command = commands[key]
        command.extend(["--queries-file", task["queries_file"], "--countries-file", task["countries_file"]])
    export_dir = task_path(task, "db_path").parent
    commands["export"] = ["python3", "-m", "trade_outreach.cli", "export-csv", str(export_dir / "drafts.csv")]
    commands["export_leads"] = ["python3", "-m", "trade_outreach.cli", "export-leads-csv", str(export_dir / "leads.csv")]
    return commands.get(command_id)


def run_command(command_id: str, task: dict) -> dict:
    command = task_command(command_id, task)
    if not command:
        return {"ok": False, "output": f"Unknown command: {command_id}"}
    if command_id in {"send_real", "auto_pipeline", "regenerate_unsent"}:
        return start_background_command(command, task=task, kind=command_id)
    return run_custom(command, timeout=COMMAND_TIMEOUTS.get(command_id, DEFAULT_COMMAND_TIMEOUT), task=task)


def task_summaries() -> list[dict]:
    summaries = []
    for task in get_tasks():
        summary = json.loads(json.dumps(task, ensure_ascii=False))
        summary["auto_pipeline_job"] = get_active_job("auto_pipeline", task["id"])
        summaries.append(summary)
    return summaries


def selected_tasks(task_ids: object) -> list[dict]:
    if not isinstance(task_ids, list):
        return []
    selected_ids = {str(task_id) for task_id in task_ids if str(task_id).strip()}
    return [task for task in get_tasks() if task["id"] in selected_ids]


def start_auto_pipelines(task_ids: object) -> dict:
    tasks = selected_tasks(task_ids)
    if not tasks:
        return {"ok": False, "output": "Select at least one valid task.", "results": []}

    results = []
    started = 0
    already_running = 0
    for task in tasks:
        existing = get_active_job("auto_pipeline", task["id"])
        if existing:
            already_running += 1
            results.append(
                {
                    "task_id": task["id"],
                    "task_name": task["name"],
                    "ok": True,
                    "status": "already_running",
                    "job_id": existing["job_id"],
                }
            )
            continue
        result = run_command("auto_pipeline", task)
        if result.get("ok"):
            started += 1
        results.append(
            {
                "task_id": task["id"],
                "task_name": task["name"],
                "ok": bool(result.get("ok")),
                "status": "started" if result.get("ok") else "failed",
                "job_id": result.get("job_id"),
                "output": result.get("output", ""),
            }
        )

    failed = len(tasks) - started - already_running
    output = (
        f"Selected {len(tasks)} task(s): started {started}, "
        f"already running {already_running}, failed {failed}."
    )
    return {"ok": failed == 0, "output": output, "results": results}


def stop_auto_pipelines(task_ids: object) -> dict:
    tasks = selected_tasks(task_ids)
    if not tasks:
        return {"ok": False, "output": "Select at least one valid task.", "results": []}

    results = []
    stopped = 0
    not_running = 0
    for task in tasks:
        active = get_active_job("auto_pipeline", task["id"])
        if not active:
            not_running += 1
            results.append(
                {
                    "task_id": task["id"],
                    "task_name": task["name"],
                    "ok": True,
                    "status": "not_running",
                }
            )
            continue
        result = stop_job(active["job_id"])
        if not result.get("running"):
            stopped += 1
        results.append(
            {
                "task_id": task["id"],
                "task_name": task["name"],
                "ok": bool(result.get("ok")),
                "status": "stopped" if not result.get("running") else "stopping",
                "job_id": active["job_id"],
            }
        )

    return {
        "ok": True,
        "output": f"Selected {len(tasks)} task(s): stopped {stopped}, not running {not_running}.",
        "results": results,
    }


def persist_jobs_locked() -> None:
    try:
        write_job_records(JOBS_FILE, list(JOBS.values()))
    except OSError as exc:
        print(f"Could not persist dashboard job registry: {exc}", flush=True)


def refresh_jobs() -> None:
    changed = False
    with JOBS_LOCK:
        for job in JOBS.values():
            if not job.get("running"):
                continue
            process = job.get("process")
            if process is not None:
                returncode = process.poll()
                alive = returncode is None
            else:
                alive = process_is_running(int(job.get("pid", 0) or 0))
                returncode = job.get("returncode")
            if not alive:
                job["running"] = False
                job["returncode"] = returncode
                job["ok"] = returncode in {None, 0}
                changed = True
        if changed:
            persist_jobs_locked()


def recover_background_jobs() -> None:
    loaded = load_job_records(JOBS_FILE)
    tasks = get_tasks()
    discovered = discover_existing_auto_pipelines(ROOT, tasks)
    with JOBS_LOCK:
        JOBS.clear()
        for record in loaded:
            job_id = str(record.get("id", "")).strip()
            if not job_id:
                continue
            record["process"] = None
            record["running"] = bool(record.get("running")) and process_is_running(int(record.get("pid", 0) or 0))
            JOBS[job_id] = record

        active_keys = {
            (job.get("task_id"), job.get("kind"))
            for job in JOBS.values()
            if job.get("running")
        }
        for item in discovered:
            key = (item["task_id"], "auto_pipeline")
            if key in active_keys:
                continue
            job_id = f"recovered-{item['task_id']}-{item['pid']}"
            JOBS[job_id] = {
                "id": job_id,
                "kind": "auto_pipeline",
                "task_id": item["task_id"],
                "task_name": item["task_name"],
                "command": item["command"],
                "pid": item["pid"],
                "pgid": item["pgid"],
                "log_path": "",
                "started_at": "",
                "running": True,
                "returncode": None,
                "ok": True,
                "process": None,
                "output_note": (
                    "Recovered an Auto Pipeline that was already running before this Dashboard started. "
                    "Earlier in-memory output is unavailable; future jobs use persistent log files.\n"
                ),
            }
            active_keys.add(key)
        persist_jobs_locked()


def start_background_command(command: list[str], task: dict | None = None, kind: str = "background") -> dict:
    refresh_jobs()
    exclusive_kinds = {"send_real", "auto_pipeline", "regenerate_unsent"}
    task_id = str(task.get("id", "")) if task else ""
    task_name = str(task.get("name", "")) if task else ""
    conflict_group = "real_send" if kind in exclusive_kinds else kind
    starting_key = (task_id, conflict_group)
    with JOBS_LOCK:
        if starting_key in STARTING_JOBS:
            return {
                "ok": False,
                "output": f"A {kind} job is already starting for task {task_name or task_id or '(unassigned)'}."
            }
        for job in JOBS.values():
            same_task = job.get("task_id", "") == task_id
            conflicts = same_task and (
                job.get("kind") == kind or (kind in exclusive_kinds and job.get("kind") in exclusive_kinds)
            )
            if conflicts and job.get("running"):
                return {
                    "ok": False,
                    "output": (
                        f"A {job.get('kind')} job is already running for task "
                        f"{task_name or task_id or '(unassigned)'}. Wait for it to finish before starting another."
                    ),
                    "job_id": job["id"],
                }
        STARTING_JOBS.add(starting_key)
    job_id = str(uuid.uuid4())
    JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = JOB_LOG_DIR / f"{task_id or 'shared'}-{kind}-{job_id[:8]}.log"
    try:
        env = os.environ.copy()
        if task:
            env.update(task_env(task))
        with log_path.open("a", encoding="utf-8") as log_handle:
            log_handle.write(
                f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                f"Starting {kind} for {task_name or task_id or '(unassigned)'}\n"
            )
            log_handle.flush()
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                text=True,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                bufsize=1,
                start_new_session=True,
            )
    except Exception as exc:
        with JOBS_LOCK:
            STARTING_JOBS.discard(starting_key)
        return {"ok": False, "output": f"Failed to start command: {exc}"}
    job = {
        "id": job_id,
        "kind": kind,
        "task_id": task_id,
        "task_name": task_name,
        "command": " ".join(command),
        "pid": process.pid,
        "pgid": process.pid,
        "log_path": str(log_path),
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "output_note": f"Started {kind} job {job_id} for task {task_name or task_id or '(unassigned)'}.\n",
        "running": True,
        "returncode": None,
        "process": process,
    }
    with JOBS_LOCK:
        STARTING_JOBS.discard(starting_key)
        JOBS[job_id] = job
        persist_jobs_locked()
    threading.Thread(target=monitor_background_job, args=(job_id, process), daemon=True).start()
    return {"ok": True, "output": job["output_note"], "job_id": job_id, "running": True}


def monitor_background_job(job_id: str, process: subprocess.Popen) -> None:
    returncode = process.wait()
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job["running"] = False
            job["returncode"] = returncode
            job["ok"] = returncode == 0
            kind = job.get("kind", "background")
            job["process"] = None
            log_path = job.get("log_path", "")
            if log_path:
                try:
                    with Path(log_path).open("a", encoding="utf-8") as handle:
                        handle.write(f"\n{kind} job finished with exit code {returncode}.\n")
                except OSError:
                    pass
            persist_jobs_locked()


def stop_job(job_id: str) -> dict:
    if not job_id:
        return {"ok": False, "output": "Job id is required.", "running": False}
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return {"ok": False, "output": "Job not found.", "running": False}
        process = job.get("process")
        pid = int(job.get("pid", 0) or 0)
        pgid = int(job.get("pgid", 0) or pid)
        if not job.get("running") or not process_is_running(pid):
            job["running"] = False
            persist_jobs_locked()
            return {"ok": True, "output": get_job_output(job) + "\nJob is not running.\n", "running": False}
        job["output_note"] = job.get("output_note", "") + "\nStop requested from dashboard.\n"
        persist_jobs_locked()
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception as exc:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job:
                job["output_note"] = job.get("output_note", "") + f"Stop request failed: {exc}\n"
                persist_jobs_locked()
        return get_job(job_id)

    deadline = time.time() + 5
    while time.time() < deadline:
        if process is not None:
            stopped = process.poll() is not None
        else:
            stopped = not process_is_running(pid)
        if stopped:
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job:
                    job["running"] = False
                    persist_jobs_locked()
            return get_job(job_id)
        time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job:
                job["running"] = False
                job["output_note"] = job.get("output_note", "") + "Background job did not stop quickly; force killed it.\n"
                persist_jobs_locked()
    except ProcessLookupError:
        pass
    return get_job(job_id)


def get_job_output(job: dict) -> str:
    note = str(job.get("output_note", ""))
    log = read_log_tail(str(job.get("log_path", "")))
    return (note + log).strip() or "(no output available)"


def get_job(job_id: str) -> dict:
    refresh_jobs()
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return {"ok": False, "output": "Job not found.", "running": False}
        return {
            "ok": job.get("ok", True),
            "output": get_job_output(job),
            "running": bool(job.get("running")),
            "returncode": job.get("returncode"),
            "job_id": job_id,
        }


def get_active_job(kind: str, task_id: str | None = None) -> dict | None:
    refresh_jobs()
    with JOBS_LOCK:
        for job_id, job in JOBS.items():
            matches_task = task_id is None or job.get("task_id") == task_id
            if job.get("kind") == kind and matches_task and job.get("running"):
                return {
                    "job_id": job_id,
                    "kind": kind,
                    "task_id": job.get("task_id", ""),
                    "task_name": job.get("task_name", ""),
                    "pid": job.get("pid"),
                    "started_at": job.get("started_at", ""),
                    "running": True,
                }
    return None


def run_custom(command: list[str], timeout: int = DEFAULT_COMMAND_TIMEOUT, task: dict | None = None) -> dict:
    env = os.environ.copy()
    if task:
        env.update(task_env(task))
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "output": f"Command timed out: {' '.join(command)}\n{exc}"}
    output = (result.stdout or "") + (result.stderr or "")
    return {"ok": result.returncode == 0, "output": output.strip() or "(no output)", "returncode": result.returncode}


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    recover_background_jobs()
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    server.serve_forever()


TASKS_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>B2B Outreach Tasks</title>
  <style>
    body { margin: 0; font-family: Inter, system-ui, sans-serif; background: #f5f7f8; color: #1d2428; }
    header { padding: 18px 24px; background: #fff; border-bottom: 1px solid #d9e0e4; display:flex; justify-content:space-between; align-items:center; }
    h1 { margin: 0; font-size: 20px; }
    main { max-width: 980px; margin: 0 auto; padding: 18px; display: grid; gap: 14px; }
    section, .task { background: #fff; border: 1px solid #d9e0e4; border-radius: 8px; }
    section { padding: 14px; }
    .task { padding: 12px; display:grid; grid-template-columns: auto minmax(0,1fr) auto auto; gap:8px; align-items:center; }
    input { height: 36px; border: 1px solid #d9e0e4; border-radius: 6px; padding: 0 10px; font: inherit; }
    input.task-select { width: 18px; height: 18px; }
    button, a.button { height: 36px; border: 1px solid #d9e0e4; background:#eef4f6; border-radius:6px; padding:0 12px; font-weight:700; cursor:pointer; color:#1d2428; text-decoration:none; display:inline-flex; align-items:center; }
    .primary { background:#256f8f !important; border-color:#256f8f !important; color:white !important; }
    .danger { color:#a33a2f; border-color:#d8aaa6; background:#fff4f3; }
    .row { display:flex; gap:8px; flex-wrap:wrap; }
    .muted { color:#60717b; font-size:12px; }
    .status { display:inline-flex; margin-top:6px; border-radius:999px; padding:3px 8px; background:#e8eef1; color:#60717b; font-size:12px; font-weight:700; }
    .status.running { background:#e2f3e8; color:#237a4b; }
    #multiOutput { margin-top:10px; min-height:18px; }
  </style>
</head>
<body>
  <header><h1>B2B Outreach Tasks</h1><div class="muted">Each task has its own leads, drafts, queries, countries, and company profile.</div></header>
  <main>
    <section>
      <div class="row">
        <input id="newTaskName" placeholder="new task name, e.g. Valve distributors">
        <button class="primary" onclick="createTask()">Create Task</button>
      </div>
    </section>
    <section>
      <div><b>Run multiple tasks together</b></div>
      <div class="muted" style="margin:6px 0 10px">Select steel pipe, valve, or any future tasks. Each selected task runs in its own process and keeps its own database.</div>
      <div class="row">
        <button onclick="selectAllTasks(true)">Select All</button>
        <button onclick="selectAllTasks(false)">Clear</button>
        <button class="primary" onclick="startSelectedTasks()">Start Selected Together</button>
        <button class="danger" onclick="stopSelectedTasks()">Stop Selected</button>
      </div>
      <div id="multiOutput" class="muted">No action yet.</div>
    </section>
    <div id="tasks"></div>
  </main>
  <script>
    let tasks = [];
    const selectedTaskIds = new Set();
    async function refresh() {
      const res = await fetch('/api/tasks');
      const data = await res.json();
      tasks = data.tasks || [];
      render();
    }
    function render() {
      document.getElementById('tasks').innerHTML = tasks.map(t => `
        <div class="task">
          <input class="task-select" type="checkbox" aria-label="Select ${escapeAttr(t.name)}" ${selectedTaskIds.has(t.id) ? 'checked' : ''} onchange="toggleTask('${t.id}', this.checked)">
          <div>
            <input id="name-${t.id}" value="${escapeAttr(t.name)}">
            <div class="muted">${escapeHtml(t.id)} | ${escapeHtml(t.db_path)}</div>
            <span class="status ${t.auto_pipeline_job && t.auto_pipeline_job.running ? 'running' : ''}">${t.auto_pipeline_job && t.auto_pipeline_job.running ? 'Auto Pipeline running' : 'Idle'}</span>
          </div>
          <a class="button primary" href="/dashboard?task=${encodeURIComponent(t.id)}">Open Dashboard</a>
          <div class="row">
            ${t.auto_pipeline_job && t.auto_pipeline_job.running
              ? `<button class="danger" onclick="stopTasks(['${t.id}'])">Stop</button>`
              : `<button onclick="startTasks(['${t.id}'])">Start</button>`}
            <button onclick="renameTask('${t.id}')">Rename</button>
            <button class="danger" onclick="deleteTask('${t.id}')">Delete</button>
          </div>
        </div>
      `).join('');
    }
    function toggleTask(id, checked) {
      if (checked) selectedTaskIds.add(id);
      else selectedTaskIds.delete(id);
    }
    function selectAllTasks(checked) {
      selectedTaskIds.clear();
      if (checked) tasks.forEach(t => selectedTaskIds.add(t.id));
      render();
    }
    async function startSelectedTasks() {
      await startTasks([...selectedTaskIds]);
    }
    async function stopSelectedTasks() {
      await stopTasks([...selectedTaskIds]);
    }
    async function startTasks(taskIds) {
      if (!taskIds.length) {
        document.getElementById('multiOutput').textContent = 'Select at least one task first.';
        return;
      }
      const names = tasks.filter(t => taskIds.includes(t.id)).map(t => t.name).join(', ');
      if (!confirm(`Start Auto Pipeline concurrently for: ${names}? Auto approval and real SMTP sending are enabled by the existing Auto Pipeline settings.`)) return;
      document.getElementById('multiOutput').textContent = 'Starting selected tasks...';
      const res = await fetch('/api/tasks-auto-start', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({task_ids:taskIds})
      });
      const data = await res.json();
      document.getElementById('multiOutput').textContent = data.output || 'Start request completed.';
      await refresh();
    }
    async function stopTasks(taskIds) {
      if (!taskIds.length) {
        document.getElementById('multiOutput').textContent = 'Select at least one task first.';
        return;
      }
      const names = tasks.filter(t => taskIds.includes(t.id)).map(t => t.name).join(', ');
      if (!confirm(`Stop Auto Pipeline for: ${names}?`)) return;
      document.getElementById('multiOutput').textContent = 'Stopping selected tasks...';
      const res = await fetch('/api/tasks-auto-stop', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({task_ids:taskIds})
      });
      const data = await res.json();
      document.getElementById('multiOutput').textContent = data.output || 'Stop request completed.';
      await refresh();
    }
    async function createTask() {
      const name = document.getElementById('newTaskName').value.trim();
      if (!name) return;
      const res = await fetch('/api/task-create', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
      const data = await res.json();
      if (data.task) location.href = `/dashboard?task=${encodeURIComponent(data.task.id)}`;
    }
    async function renameTask(id) {
      const name = document.getElementById(`name-${id}`).value.trim();
      await fetch('/api/task-rename', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({task_id:id, name})});
      await refresh();
    }
    async function deleteTask(id) {
      if (!confirm('Delete this task from the task list? Data files remain on disk for safety.')) return;
      await fetch('/api/task-delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({task_id:id})});
      await refresh();
    }
    function escapeHtml(value) { return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }
    function escapeAttr(value) { return escapeHtml(value); }
    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>B2B Outreach Dashboard</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7f8;
      --panel: #ffffff;
      --line: #d9e0e4;
      --text: #1d2428;
      --muted: #60717b;
      --accent: #256f8f;
      --accent-dark: #15516c;
      --ok: #237a4b;
      --warn: #9a5b00;
      --bad: #a33a2f;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      letter-spacing: 0;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 14px 18px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 3;
    }
    h1 { margin: 0; font-size: 18px; font-weight: 700; }
    main {
      display: grid;
      grid-template-columns: 280px minmax(0, 1fr);
      gap: 14px;
      padding: 14px;
    }
    aside, section, .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    aside { padding: 12px; height: calc(100vh - 86px); position: sticky; top: 72px; overflow: auto; }
    .content { display: grid; gap: 14px; min-width: 0; }
    .metrics-panel { padding: 12px; }
    .metrics-title { font-size: 13px; font-weight: 800; margin: 0 0 10px; color: var(--muted); text-transform: uppercase; }
    .stats { display: grid; grid-template-columns: repeat(5, minmax(120px, 1fr)); gap: 10px; }
    .diagnostics { grid-template-columns: repeat(2, minmax(160px, 1fr)); }
    .stat { padding: 12px; }
    .stat b { display: block; font-size: 24px; margin-top: 4px; }
    .muted { color: var(--muted); font-size: 12px; }
    .controls { display: grid; gap: 8px; }
    button {
      height: 34px;
      border: 1px solid var(--line);
      background: #eef4f6;
      color: var(--text);
      border-radius: 6px;
      cursor: pointer;
      font-weight: 600;
    }
    button.primary { background: var(--accent); border-color: var(--accent); color: white; }
    button:hover { border-color: var(--accent); }
    button:disabled { opacity: .55; cursor: wait; }
    button.danger { color: var(--bad); border-color: #d8aaa6; background: #fff4f3; }
    .grid2 { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px; }
    section { overflow: hidden; }
    section h2 {
      margin: 0;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      font-size: 15px;
    }
    .table-wrap { overflow: auto; max-height: 420px; }
    table { border-collapse: collapse; width: 100%; min-width: 900px; font-size: 13px; }
    th, td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { background: #f0f4f6; position: sticky; top: 0; z-index: 1; }
    tr:hover td { background: #f8fbfc; }
    .pill { display: inline-flex; align-items: center; height: 22px; padding: 0 8px; border-radius: 999px; background: #e8eef1; font-size: 12px; white-space: nowrap; }
    .pill.ok { background: #e2f3e8; color: var(--ok); }
    .pill.warn { background: #fff1d8; color: var(--warn); }
    .pill.sent { background: #dff0ff; color: #15516c; }
    .email-list { display: flex; flex-wrap: wrap; gap: 4px; max-width: 360px; }
    .email-add { display: grid; grid-template-columns: minmax(160px, 1fr) 54px; gap: 6px; margin-top: 6px; max-width: 360px; }
    .email-add input, .email-add button { height: 30px; }
    pre {
      margin: 0;
      padding: 12px;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
      max-height: 360px;
      overflow: auto;
      background: #f8fafb;
    }
    .output { max-height: 260px; background: #101820; color: #dfe8ec; }
    .row-actions { display: flex; gap: 6px; flex-wrap: wrap; }
    input, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 9px;
      font: inherit;
    }
    input {
      height: 34px;
      padding-top: 0;
      padding-bottom: 0;
    }
    textarea {
      min-height: 74px;
      resize: vertical;
    }
    .panel-title {
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      color: var(--muted);
      margin: 14px 0 8px;
    }
    .compact-grid {
      display: grid;
      grid-template-columns: 1fr 86px;
      gap: 8px;
    }
    .list-editor { padding: 12px; display: grid; gap: 8px; }
    .list-row { display: grid; grid-template-columns: minmax(0, 1fr) 72px; gap: 8px; align-items: center; }
    .list-value {
      min-height: 30px;
      display: flex;
      align-items: center;
      padding: 5px 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #f8fafb;
      word-break: break-word;
    }
    .list-add { display: grid; grid-template-columns: minmax(0, 1fr) 72px; gap: 8px; margin-top: 6px; }
    .list-group-title { font-size: 12px; font-weight: 800; color: var(--muted); margin-top: 8px; text-transform: uppercase; }
    .preset-add { display: grid; grid-template-columns: minmax(0, 1fr) 118px; gap: 8px; margin-top: 6px; }
    select {
      width: 100%;
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 9px;
      font: inherit;
      background: white;
    }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      aside { position: static; height: auto; }
      .stats, .grid2 { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1><span id="taskName">B2B Outreach Dashboard</span></h1>
    <a href="/" class="muted">Tasks</a>
    <div class="muted" id="configLine">Loading...</div>
  </header>
  <main>
    <aside>
      <div class="controls">
        <button onclick="refresh()">Refresh</button>
        <button onclick="runCmd('test_llm')">Test LLM</button>
        <button onclick="runCmd('test_smtp')">Test SMTP</button>
        <div class="panel-title">Your Company</div>
        <input id="settingsTargetRegion" placeholder="your company location/region, e.g. Hong Kong">
        <div style="height:8px"></div>
        <input id="settingsCompanyName" placeholder="your company name">
        <div style="height:8px"></div>
        <textarea id="settingsCompanyAddress" placeholder="your company mailing address"></textarea>
        <div style="height:8px"></div>
        <textarea id="settingsBusinessSummary" placeholder="business summary used in email"></textarea>
        <div style="height:8px"></div>
        <button onclick="saveSettings()">Save Required Info</button>
        <div style="height:8px"></div>
        <div id="profileItems"></div>
        <div class="panel-title">Preset Actions</div>
        <button onclick="generateQueries()">Generate Target Queries</button>
        <button class="primary" onclick="runCmd('discover_agent_targets')">Agent Explore Targets</button>
        <button onclick="runCmd('crawl_emails')">Crawl All Missing Emails</button>
        <button onclick="runCmd('score')">Score / Rescore</button>
        <button onclick="runCmd('draft')">Generate All Missing Drafts</button>
        <button onclick="runCmd('regenerate_unsent')">Regenerate Unsent Drafts</button>
        <button id="stopRegenerateBtn" class="danger" onclick="stopRegeneration()" disabled>Stop Draft Regeneration</button>
        <button onclick="runCmd('export')">Export CSV</button>
        <button onclick="runCmd('export_leads')">Export Leads CSV</button>
        <input id="importLeadsPath" placeholder="CSV path to import leads">
        <button onclick="importLeads()">Import Leads CSV</button>
        <button onclick="runCmd('approve_preview')">Approve Preview</button>
        <button onclick="runCmd('approve_execute')">Approve Batch</button>
        <button onclick="runCmd('send_dry_run')">Send Dry Run</button>
        <button class="danger" onclick="runCmd('send_real')">Send All Approved</button>
        <button class="danger" onclick="runCmd('auto_pipeline')">Auto Pipeline</button>
        <button id="stopAutoPipelineBtn" class="danger" onclick="stopCurrentJob()" disabled>Stop Auto Pipeline</button>
      </div>
      <div style="height:12px"></div>
      <input id="manualEmail" placeholder="manual recipient email">
      <div style="height:8px"></div>
      <input id="manualCompany" placeholder="company name optional">
      <div style="height:8px"></div>
      <button onclick="manualEmailDraft()">Create Draft</button>
      <div style="height:12px"></div>
      <input id="testRecipient" placeholder="test recipient email" value="test@example.com">
      <div style="height:8px"></div>
      <div class="muted">Use row buttons below to send a test copy for a draft.</div>
    </aside>
    <div class="content">
      <div class="card metrics-panel">
        <div class="metrics-title">Status Metrics</div>
        <div class="stats" id="statusStats"></div>
      </div>
      <div class="card metrics-panel">
        <div class="metrics-title">Diagnostic Metrics</div>
        <div class="stats diagnostics" id="diagnosticStats"></div>
      </div>
      <section>
        <h2>Command Output</h2>
        <pre class="output" id="output">(idle)</pre>
      </section>
      <section>
        <h2>Discovery History</h2>
        <div class="table-wrap"><table id="discoveryTable"></table></div>
      </section>
      <section>
        <h2>Leads</h2>
        <div class="table-wrap"><table id="leadsTable"></table></div>
      </section>
      <section>
        <h2>Drafts</h2>
        <div class="table-wrap"><table id="draftsTable"></table></div>
      </section>
      <div class="grid2">
        <section><h2>Prompt</h2><pre id="prompt"></pre></section>
        <section><h2>Email Template</h2><pre id="template"></pre></section>
      </div>
      <div class="grid2">
        <section><h2>Queries</h2><div id="queries"></div></section>
        <section><h2>Countries</h2><div id="countries"></div></section>
      </div>
    </div>
  </main>
  <script>
    let state = null;
    let currentJobId = null;
    let currentAutoPipelineJobId = null;
    let currentRegenerateJobId = null;
    const taskId = new URLSearchParams(location.search).get('task') || 'default';
    async function refresh() {
      const res = await fetch(`/api/state?task=${encodeURIComponent(taskId)}`);
      state = await res.json();
      render();
    }
    function render() {
      const activeAutoJob = state.active_auto_pipeline_job;
      currentAutoPipelineJobId = activeAutoJob && activeAutoJob.running ? activeAutoJob.job_id : null;
      const activeRegenerateJob = state.active_regenerate_job;
      currentRegenerateJobId = activeRegenerateJob && activeRegenerateJob.running ? activeRegenerateJob.job_id : null;
      updateStopButton();
      const c = state.config;
      document.getElementById('taskName').textContent = c.task_name || 'B2B Outreach Dashboard';
      document.getElementById('configLine').textContent = `Company ${c.target_region || '-'} | ${c.llm} | ${c.smtp} | BCC ${c.mail_bcc || '-'} | ${c.send_daily_limit}/day | ${c.send_delay_seconds}s delay | Facebook ${c.facebook ? 'set' : '-'}`;
      const s = state.stats;
      document.getElementById('statusStats').innerHTML = [
        stat('Companies', s.total_leads),
        stat('Companies With Email', s.leads_with_valid_email),
        stat('Drafts', s.drafts),
        stat('Approved Unsent', s.approved_unsent),
        stat('Pending Review', s.pending_unsent),
        stat('Sent', s.sent),
        stat('Global Send Left Today', s.remaining_today, `${s.global_sent_today || 0}/${s.daily_limit || 0} sent across all tasks`),
      ].join('');
      document.getElementById('diagnosticStats').innerHTML = [
        stat('Email Crawl Success', `${s.email_crawl_rate}%`),
        stat('Bounce Rate', s.bounce_rate === null ? 'N/A' : `${s.bounce_rate}%`, s.bounce_rate === null ? 'No bounce data recorded' : `${s.bounce_count} bounced`),
      ].join('');
      renderLeads();
      renderDrafts();
      renderDiscoveryRuns();
      document.getElementById('prompt').textContent = state.prompt;
      document.getElementById('template').textContent = state.template;
      renderListEditor('queries', state.query_items || []);
      renderListEditor('countries', state.country_items || []);
      syncSettingsForm(c);
      renderProfileItems(c.company_items || []);
    }
    function syncSettingsForm(c) {
      setInputValue('settingsTargetRegion', c.target_region || '');
      setInputValue('settingsCompanyName', c.company_name || '');
      setInputValue('settingsCompanyAddress', c.company_address || '');
      setInputValue('settingsBusinessSummary', c.company_business_summary || '');
    }
    function setInputValue(id, value) {
      const el = document.getElementById(id);
      if (document.activeElement !== el) el.value = value;
    }
    function stat(label, value, note = '') {
      return `<div class="card stat"><span class="muted">${escapeHtml(label)}</span><b>${value}</b>${note ? `<span class="muted">${escapeHtml(note)}</span>` : ''}</div>`;
    }
    function renderListEditor(listName, items) {
      const countryPreset = listName === 'countries' ? renderCountryPresetImporter() : '';
      const rows = listName === 'countries' ? renderCountryGroups() : items.map(item => `
        <div class="list-row">
          <div class="list-value">${escapeHtml(item)}</div>
          <button onclick='deleteListItem(${JSON.stringify(listName)}, ${JSON.stringify(item)})'>Delete</button>
        </div>
      `).join('');
      document.getElementById(listName).innerHTML = `
        <div class="list-editor">
          ${countryPreset}
          ${rows || '<div class="muted">No items yet</div>'}
          <div class="list-add">
            <input id="${listName}-new" placeholder="add ${listName === 'queries' ? 'query' : 'country'}">
            <button onclick='addListItem(${JSON.stringify(listName)})'>Add</button>
          </div>
        </div>
      `;
    }
    function renderCountryGroups() {
      const groups = state.country_groups || [];
      return groups.map(group => `
        <div class="list-group-title">${escapeHtml(group.continent)} (${(group.countries || []).length})</div>
        ${(group.countries || []).map(item => `
          <div class="list-row">
            <div class="list-value">${countryStatusHtml(item)}</div>
            <button onclick='deleteListItem("countries", ${JSON.stringify(item)})'>Delete</button>
          </div>
        `).join('')}
      `).join('');
    }
    function countryStatusHtml(country) {
      const item = (state.country_discovery || {})[country] || {};
      const cooldown = item.cooldown_active ? ` · cooling until ${item.cooldown_until || '-'}` : '';
      const last = item.last_searched_at ? ` · last ${item.last_searched_at}` : ' · not searched by the new scheduler yet';
      return `${escapeHtml(country)} <span class="muted">leads ${Number(item.lead_count || 0)}/${Number(item.target_count || 0)} · ${escapeHtml(item.tier || '-')} · round ${Number(item.rounds_completed || 0)} · no-new ${Number(item.no_new_rounds || 0)}${escapeHtml(cooldown)}${escapeHtml(last)}</span>`;
    }
    function renderCountryPresetImporter() {
      const presets = state.country_presets || {};
      const options = Object.entries(presets)
        .map(([name, count]) => `<option value="${escapeAttr(name)}">${escapeHtml(name)} (${count})</option>`)
        .join('');
      return `
        <div class="preset-add">
          <select id="countryPreset"><option value="">Select continent...</option>${options}</select>
          <button onclick="addCountryPreset()">Import Preset</button>
        </div>
      `;
    }
    function renderProfileItems(items) {
      const rows = items.map((item, index) => `
        <div class="list-row">
          <div class="list-value"><b>${escapeHtml(item.label || 'Info')}</b>: ${escapeHtml(item.value || '')}</div>
          <button onclick="deleteProfileItem(${index})">Delete</button>
        </div>
      `).join('');
      document.getElementById('profileItems').innerHTML = `
        <div class="list-editor" style="padding:0">
          ${rows || '<div class="muted">No optional company info yet</div>'}
          <input id="profileItemLabel" placeholder="label, e.g. Facebook">
          <input id="profileItemValue" placeholder="value, e.g. https://facebook.com/...">
          <button onclick="addProfileItem()">Add Company Info Item</button>
        </div>
      `;
    }
    function renderLeads() {
      const rows = state.leads.map(l => `
        <tr>
          <td>#${l.id}</td>
          <td><b>${escapeHtml(l.company_name || '')}</b><br><a href="${escapeAttr(l.website)}" target="_blank">${escapeHtml(l.website)}</a></td>
          <td>${escapeHtml(l.country || '')}</td>
          <td><span class="pill">${escapeHtml(l.status)}</span></td>
          <td>${l.score}</td>
          <td>
            <div class="email-list">${l.emails.map(e => `<span class="pill ${e.startsWith('info@') || e.startsWith('sales@') ? 'ok' : ''}">${escapeHtml(e)}</span>`).join('')}</div>
            <div class="email-add">
              <input id="leadEmail-${l.id}" placeholder="add email">
              <button onclick="addLeadEmail(${l.id})">Add</button>
            </div>
          </td>
          <td>${escapeHtml(l.llm_note || '')}</td>
        </tr>`).join('');
      document.getElementById('leadsTable').innerHTML = `
        <thead><tr><th>ID</th><th>Company</th><th>Country</th><th>Status</th><th>Score</th><th>Emails</th><th>Score Note</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="7">No leads yet</td></tr>'}</tbody>`;
    }
    function renderDrafts() {
      const rows = state.drafts.map(d => `
        <tr>
          <td>#${d.id}</td>
          <td><b>${escapeHtml(d.company_name || '')}</b><br>${escapeHtml(d.recipient)}</td>
          <td>${d.score}</td>
          <td>${draftStatus(d)}</td>
          <td><b>${escapeHtml(d.subject)}</b><pre style="max-height:150px">${escapeHtml(d.body)}</pre></td>
          <td><div class="row-actions">${draftActions(d)}</div></td>
        </tr>`).join('');
      document.getElementById('draftsTable').innerHTML = `
        <thead><tr><th>ID</th><th>Recipient</th><th>Score</th><th>Status</th><th>Draft</th><th>Actions</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="6">No drafts yet</td></tr>'}</tbody>`;
    }
    function renderDiscoveryRuns() {
      const rows = (state.discovery_runs || []).map(item => `
        <tr>
          <td>${escapeHtml(item.searched_at || '')}</td>
          <td>${escapeHtml(item.country || '')}</td>
          <td>R${Number(item.round_no || 0)} ${escapeHtml(item.phase || '')}</td>
          <td>${escapeHtml(item.query || '')}</td>
          <td>${Number(item.result_count || 0)}</td>
          <td>${Number(item.new_website_count || 0)}</td>
          <td>${escapeHtml(item.error || '')}</td>
        </tr>
      `).join('');
      document.getElementById('discoveryTable').innerHTML = `
        <thead><tr><th>Last Search</th><th>Country</th><th>Round</th><th>Query</th><th>Results</th><th>New</th><th>Error</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="7">No recorded executed discovery searches yet</td></tr>'}</tbody>`;
    }
    function draftStatus(d) {
      if (d.sent_at) {
        return `<span class="pill sent">sent</span><br><span class="muted">${escapeHtml(d.sent_at)}</span>`;
      }
      return `<span class="pill ${d.approved ? 'ok' : 'warn'}">${d.approved ? 'approved' : 'pending'}</span>`;
    }
    function draftActions(d) {
      const testButtons = `
        <button onclick="sendTest(${d.id}, true)">Test Dry</button>
        <button onclick="sendTest(${d.id}, false)">Send Test</button>
      `;
      if (d.sent_at) return testButtons;
      return `
        <button onclick="approveDraft(${d.id})">Approve</button>
        <button onclick="unapproveDraft(${d.id})">Unapprove</button>
        <button onclick="regenerateDraft(${d.id})">Regenerate</button>
        ${d.approved ? `<button class="danger" onclick="sendDraft(${d.id})">Send</button>` : ''}
        ${testButtons}
      `;
    }
    async function runCmd(command) {
      if (command === 'discover_agent_targets' && !confirm('Run the fair discovery scheduler for the next countries in the current round? Expansion rounds use unused keywords, local-language queries, and major cities. This may use more Google Places requests.')) return;
      if (command === 'regenerate_unsent' && !confirm('Regenerate all unsent drafts now? Already sent emails will not be changed. Regenerated drafts will be marked pending for review.')) return;
      if (command === 'approve_execute' && !confirm('Approve the preview batch of drafts?')) return;
      if (command === 'send_real' && !confirm('Really send up to 100 approved unsent emails now? The interval is 800 seconds per email. If the computer sleeps or the process is suspended, this batch will stop instead of continuing late.')) return;
      setBusy(true);
      document.getElementById('output').textContent = `Running ${command}...`;
      const res = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({command, task_id: taskId})});
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      if (data.ok && data.job_id) {
        currentJobId = data.job_id;
        if (command === 'auto_pipeline') currentAutoPipelineJobId = data.job_id;
        if (command === 'regenerate_unsent') currentRegenerateJobId = data.job_id;
        updateStopButton();
        await pollJob(data.job_id);
        return;
      }
      setBusy(false);
      await refresh();
    }
    async function pollJob(jobId) {
      const outputEl = document.getElementById('output');
      let running = true;
      while (running) {
        await new Promise(resolve => setTimeout(resolve, 5000));
        const res = await fetch(`/api/job?id=${encodeURIComponent(jobId)}`);
        const data = await res.json();
        outputEl.textContent = data.output;
        outputEl.scrollTop = outputEl.scrollHeight;
        running = Boolean(data.running);
        await refresh();
      }
      if (jobId === currentJobId) currentJobId = null;
      if (jobId === currentAutoPipelineJobId) currentAutoPipelineJobId = null;
      if (jobId === currentRegenerateJobId) currentRegenerateJobId = null;
      setBusy(false);
      updateStopButton();
      await refresh();
    }
    async function stopCurrentJob() {
      if (!currentAutoPipelineJobId) return;
      if (!confirm('Stop the running Auto Pipeline job now?')) return;
      const res = await fetch('/api/job-stop', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({job_id: currentAutoPipelineJobId})
      });
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      if (!data.running) {
        if (data.job_id === currentJobId) currentJobId = null;
        currentAutoPipelineJobId = null;
        setBusy(false);
        updateStopButton();
        await refresh();
      }
    }
    async function stopRegeneration() {
      if (!currentRegenerateJobId) return;
      if (!confirm('Stop draft regeneration after the current request? Drafts already regenerated will remain pending for review.')) return;
      const jobId = currentRegenerateJobId;
      const res = await fetch('/api/job-stop', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({job_id: jobId})
      });
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      if (!data.running) {
        if (jobId === currentJobId) currentJobId = null;
        currentRegenerateJobId = null;
        setBusy(false);
        await refresh();
      }
    }
    async function generateQueries() {
      if (!confirm('Generate and append target discovery queries for this task using the configured LLM?')) return;
      setBusy(true);
      document.getElementById('output').textContent = 'Generating target queries...';
      const res = await fetch('/api/generate-queries', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({task_id: taskId})
      });
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      setBusy(false);
      await refresh();
    }
    async function addListItem(listName) {
      const input = document.getElementById(`${listName}-new`);
      const value = input.value.trim();
      if (!value) return;
      await updateListItem(listName, 'add', value);
      input.value = '';
    }
    async function deleteListItem(listName, value) {
      if (!confirm(`Delete "${value}" from ${listName}?`)) return;
      await updateListItem(listName, 'delete', value);
    }
    async function updateListItem(listName, action, value) {
      setBusy(true);
      const res = await fetch('/api/list-item', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({list: listName, action, value, task_id: taskId})
      });
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      setBusy(false);
      await refresh();
    }
    async function addCountryPreset() {
      const select = document.getElementById('countryPreset');
      const preset = select ? select.value : '';
      if (!preset) {
        document.getElementById('output').textContent = 'Select a continent preset first.';
        return;
      }
      setBusy(true);
      const res = await fetch('/api/countries-preset', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({preset, task_id: taskId})
      });
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      setBusy(false);
      await refresh();
    }
    async function saveSettings() {
      const payload = {
        target_region: document.getElementById('settingsTargetRegion').value,
        company_name: document.getElementById('settingsCompanyName').value,
        company_address: document.getElementById('settingsCompanyAddress').value,
        business_summary: document.getElementById('settingsBusinessSummary').value,
        task_id: taskId,
      };
      setBusy(true);
      const res = await fetch('/api/save-settings', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      setBusy(false);
      await refresh();
    }
    async function addProfileItem() {
      const label = document.getElementById('profileItemLabel').value.trim();
      const value = document.getElementById('profileItemValue').value.trim();
      if (!value) return;
      setBusy(true);
      const res = await fetch('/api/profile-item', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({task_id: taskId, action: 'add', label, value})
      });
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      setBusy(false);
      await refresh();
    }
    async function deleteProfileItem(index) {
      if (!confirm('Delete this company info item?')) return;
      setBusy(true);
      const res = await fetch('/api/profile-item', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({task_id: taskId, action: 'delete', index})
      });
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      setBusy(false);
      await refresh();
    }
    async function importLeads() {
      const path = document.getElementById('importLeadsPath').value.trim();
      if (!path) return;
      setBusy(true);
      document.getElementById('output').textContent = `Importing leads from ${path}...`;
      const res = await fetch('/api/import-leads', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({task_id: taskId, path})
      });
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      setBusy(false);
      await refresh();
    }
    async function approveDraft(draft_id) {
      setBusy(true);
      const res = await fetch('/api/approve', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({draft_id, task_id: taskId})});
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      setBusy(false);
      await refresh();
    }
    async function unapproveDraft(draft_id) {
      setBusy(true);
      const res = await fetch('/api/unapprove', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({draft_id, task_id: taskId})});
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      setBusy(false);
      await refresh();
    }
    async function regenerateDraft(draft_id) {
      if (!confirm('Regenerate this draft and mark it pending?')) return;
      setBusy(true);
      const res = await fetch('/api/regenerate-draft', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({draft_id, task_id: taskId})});
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      setBusy(false);
      await refresh();
    }
    async function sendTest(draft_id, dry_run) {
      const recipient = document.getElementById('testRecipient').value;
      setBusy(true);
      const res = await fetch('/api/send-test', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({draft_id, recipient, dry_run, task_id: taskId})});
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      setBusy(false);
      await refresh();
    }
    async function sendDraft(draft_id) {
      if (!confirm('Really send this approved draft now? It will include the configured BCC copy.')) return;
      setBusy(true);
      const res = await fetch('/api/send-draft', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({draft_id, task_id: taskId})});
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      setBusy(false);
      await refresh();
    }
    async function manualEmailDraft() {
      const email = document.getElementById('manualEmail').value.trim();
      const company = document.getElementById('manualCompany').value.trim();
      if (!email) return;
      setBusy(true);
      document.getElementById('output').textContent = `Creating draft for ${email}...`;
      const res = await fetch('/api/manual-email-draft', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({email, company, task_id: taskId})
      });
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      setBusy(false);
      await refresh();
    }
    async function addLeadEmail(lead_id) {
      const input = document.getElementById(`leadEmail-${lead_id}`);
      const email = input.value.trim();
      if (!email) return;
      setBusy(true);
      document.getElementById('output').textContent = `Adding ${email} to lead #${lead_id}...`;
      const res = await fetch('/api/lead-email', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({lead_id, email, task_id: taskId})
      });
      const data = await res.json();
      document.getElementById('output').textContent = data.output;
      setBusy(false);
      await refresh();
    }
    function setBusy(busy) {
      document.querySelectorAll('button').forEach(b => b.disabled = busy);
      updateStopButton();
    }
    function updateStopButton() {
      const btn = document.getElementById('stopAutoPipelineBtn');
      if (btn) btn.disabled = !currentAutoPipelineJobId;
      const regenerateBtn = document.getElementById('stopRegenerateBtn');
      if (regenerateBtn) regenerateBtn.disabled = !currentRegenerateJobId;
    }
    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function escapeAttr(value) { return escapeHtml(value); }
    refresh().then(() => {
      const job = state.active_regenerate_job;
      if (job && job.running) {
        currentJobId = job.job_id;
        setBusy(true);
        pollJob(job.job_id);
      }
    });
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    run()
