from __future__ import annotations

import argparse
import csv
import json
import re
import smtplib
import sqlite3
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from email.utils import getaddresses
from pathlib import Path

from .config import get_settings
from .db import connect, init_db, normalize_url, upsert_leads
from .discovery import discover_websites
from .discovery_schedule import (
    country_discovery_status,
    plan_country_schedule,
    record_cooldown_skips,
    record_country_round,
    record_discovery_run,
    scheduled_queries,
)
from .emailer import send_email, test_smtp
from .llm import cache_key, generate_text, personalization_prompt
from .scoring import clean_emails_for_website, is_generic_business_email, is_valid_business_email, pick_recipient, score_lead
from .send_control import acquire_send_slot, global_sent_state
from .scraper import scrape_site
from .templates import draft_body, subject_for


def sent_today_count(conn) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM email_drafts
        WHERE sent_at IS NOT NULL
          AND date(sent_at, 'localtime') = date('now', 'localtime')
        """
    ).fetchone()
    return int(row["count"] if row else 0)


def remaining_daily_send_quota(conn, settings) -> int:
    return max(settings.send_daily_limit - sent_today_count(conn), 0)


def draft_send_status(conn) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN sent_at IS NULL THEN 1 ELSE 0 END) AS unsent,
            SUM(CASE WHEN approved = 1 AND sent_at IS NULL THEN 1 ELSE 0 END) AS approved_unsent,
            SUM(CASE WHEN approved = 0 AND sent_at IS NULL THEN 1 ELSE 0 END) AS pending_unsent,
            SUM(CASE WHEN sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent
        FROM email_drafts
        """
    ).fetchone()
    unsent_rows = conn.execute(
        """
        SELECT approved, recipient
        FROM email_drafts
        WHERE sent_at IS NULL
        """
    ).fetchall()
    valid_unsent = [row for row in unsent_rows if is_valid_business_email(row["recipient"])]
    return {
        "total": int(row["total"] or 0),
        "unsent": int(row["unsent"] or 0),
        "approved_unsent": int(row["approved_unsent"] or 0),
        "pending_unsent": int(row["pending_unsent"] or 0),
        "sent": int(row["sent"] or 0),
        "valid_unsent": len(valid_unsent),
        "valid_approved_unsent": sum(1 for row in valid_unsent if row["approved"]),
        "valid_pending_unsent": sum(1 for row in valid_unsent if not row["approved"]),
    }


def run_cli_step(name: str, argv: list[str]) -> int:
    print("")
    print(f"=== {name}: python3 -m trade_outreach.cli {' '.join(argv)} ===", flush=True)
    return main(argv)


def has_sendable_drafts(status: dict[str, int], auto_approve: bool) -> bool:
    return status["valid_approved_unsent"] > 0 or (auto_approve and status["valid_pending_unsent"] > 0)


def approved_send_backlog(db_path: str) -> int:
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM email_drafts WHERE approved = 1 AND sent_at IS NULL"
        ).fetchone()[0])
    finally:
        conn.close()


def recipient_needs_smtputf8(recipient: str, settings) -> bool:
    envelope_addresses = getaddresses([settings.mail_from, settings.mail_cc, settings.mail_bcc])
    return not recipient.isascii() and all(address.isascii() for _, address in envelope_addresses)


def run_queue_worker(
    name: str,
    action,
    cycles: int,
    interval_seconds: float,
    stop_event: threading.Event,
    failures: list[str],
) -> None:
    cycle = 0
    while not stop_event.is_set():
        cycle += 1
        print(f"\n--- {name} queue cycle {cycle} ---", flush=True)
        try:
            returncode = action()
            if returncode:
                message = f"{name} queue cycle {cycle} returned exit code {returncode}."
                failures.append(message)
                print(message, flush=True)
        except Exception as exc:
            message = f"{name} queue cycle {cycle} failed: {type(exc).__name__}: {exc}"
            failures.append(message)
            print(message, flush=True)

        if cycles and cycle >= cycles:
            break
        if interval_seconds > 0:
            print(f"{name} queue sleeping {interval_seconds:g}s before its next cycle.", flush=True)
            stop_event.wait(interval_seconds)


def run_independent_auto_pipeline(args, settings) -> int:
    auto_approve = not args.no_auto_approve
    send_real = not args.dry_run
    discovery_interval = (
        settings.auto_discovery_interval_seconds
        if args.discovery_interval_seconds is None
        else args.discovery_interval_seconds
    )
    processing_interval = (
        settings.auto_processing_interval_seconds
        if args.processing_interval_seconds is None
        else args.processing_interval_seconds
    )
    send_idle = settings.auto_send_idle_seconds if args.idle_sleep_seconds is None else args.idle_sleep_seconds

    print("Auto pipeline started with independent discovery, processing, and sending queues.", flush=True)
    print(
        f"Queue intervals: discovery={discovery_interval:g}s, "
        f"processing={processing_interval:g}s, send-idle={send_idle:g}s.",
        flush=True,
    )
    print(
        "Real sending is ENABLED." if send_real else "Real sending is disabled because --dry-run was provided.",
        flush=True,
    )
    print(
        "Auto approval is ENABLED." if auto_approve else "Auto approval is disabled because --no-auto-approve was provided.",
        flush=True,
    )

    def backlog_is_full() -> bool:
        threshold = args.backlog_pause_threshold
        if threshold <= 0:
            return False
        count = approved_send_backlog(settings.db_path)
        if count >= threshold:
            print(
                f"Approved send backlog is {count} (pause threshold {threshold}); "
                "pausing discovery and processing while sending continues.",
                flush=True,
            )
            return True
        return False

    def discovery_cycle() -> int:
        if backlog_is_full():
            return 0
        discover_args = [
            "discover-batch",
            "--queries-file",
            args.queries_file,
            "--countries-file",
            args.countries_file,
            "--count",
            str(args.discover_count),
            "--limit-queries",
            str(args.limit_queries),
            "--limit-countries",
            str(args.limit_countries),
            "--execute",
        ]
        if args.least_searched_countries:
            discover_args.extend(["--least-searched-countries", str(args.least_searched_countries)])
        if args.scheduled_countries:
            discover_args.extend(["--scheduled-countries", str(args.scheduled_countries)])
        return run_cli_step("discover companies", discover_args)

    def processing_cycle() -> int:
        if backlog_is_full():
            return 0
        scrape_args = [
            "scrape",
            "--limit",
            str(args.scrape_limit),
            "--retry-failed",
            "--max-seconds",
            str(args.scrape_max_seconds),
            "--fetch-attempts",
            str(args.fetch_attempts),
            "--fetch-timeout",
            str(args.fetch_timeout),
            "--site-max-seconds",
            str(args.site_max_seconds),
            "--workers",
            str(args.scrape_workers),
        ]
        returncode = run_cli_step("scrape emails", scrape_args)
        if returncode:
            return returncode
        returncode = run_cli_step("score leads", ["score"])
        if returncode:
            return returncode
        draft_args = ["draft", "--limit", str(args.draft_limit), "--min-score", str(args.draft_min_score)]
        if args.include_personal:
            draft_args.append("--include-personal")
        if args.no_cache:
            draft_args.append("--no-cache")
        returncode = run_cli_step("generate drafts", draft_args)
        if returncode or not auto_approve:
            if not auto_approve:
                print("Drafts remain pending because --no-auto-approve was provided.", flush=True)
            return returncode
        approve_args = [
            "approve-batch",
            "--limit",
            str(args.approve_limit),
            "--min-score",
            str(args.approve_min_score),
            "--execute",
        ]
        if args.include_personal:
            approve_args.append("--include-personal")
        return run_cli_step("approve drafts", approve_args)

    def sending_cycle() -> int:
        send_args = ["send", "--limit", str(args.send_limit), "--continue-after-sleep"]
        if args.delay_seconds is not None:
            send_args.extend(["--delay-seconds", str(args.delay_seconds)])
        if not send_real:
            send_args.append("--dry-run")
        return run_cli_step("send approved drafts", send_args)

    stop_event = threading.Event()
    failures: list[str] = []
    queue_specs = []
    if not args.skip_discovery:
        queue_specs.append(("Discovery", discovery_cycle, discovery_interval))
    queue_specs.extend(
        [
            ("Processing", processing_cycle, processing_interval),
            ("Sending", sending_cycle, send_idle),
        ]
    )
    threads = [
        threading.Thread(
            target=run_queue_worker,
            args=(name, action, args.cycles, interval, stop_event, failures),
            name=f"auto-{name.lower()}",
            daemon=True,
        )
        for name, action, interval in queue_specs
    ]
    for thread in threads:
        thread.start()
    try:
        while any(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(timeout=0.5)
    except KeyboardInterrupt:
        print("Stopping all Auto Pipeline queues...", flush=True)
        stop_event.set()
        for thread in threads:
            thread.join(timeout=5)
    print(f"Auto pipeline finished. Queue errors recorded: {len(failures)}.", flush=True)
    return 1 if args.cycles and failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Steel pipe distributor outreach pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db")

    p_import = sub.add_parser("import-seeds")
    p_import.add_argument("csv_path")

    p_import_urls = sub.add_parser("import-urls")
    p_import_urls.add_argument("path")
    p_import_urls.add_argument("--source", default="manual-url-list")

    p_import_leads = sub.add_parser("import-leads-csv")
    p_import_leads.add_argument("csv_path")
    p_import_leads.add_argument("--source", default="lead-csv")

    p_discover = sub.add_parser("discover")
    p_discover.add_argument("--query", action="append", required=True)
    p_discover.add_argument("--state", action="append")
    p_discover.add_argument("--count", type=int, default=10)
    p_discover.add_argument("--execute", action="store_true")

    p_discover_europe = sub.add_parser("discover-batch", aliases=["discover-europe"])
    p_discover_europe.add_argument("--queries-file", default="examples/discovery_queries.txt")
    p_discover_europe.add_argument("--countries-file", default="examples/countries.txt")
    p_discover_europe.add_argument("--count", type=int, default=10)
    p_discover_europe.add_argument("--limit-queries", type=int)
    p_discover_europe.add_argument("--limit-countries", type=int)
    p_discover_europe.add_argument("--offset-queries", type=int, default=0)
    p_discover_europe.add_argument("--offset-countries", type=int, default=0)
    p_discover_europe.add_argument("--least-searched-countries", type=int, default=0)
    p_discover_europe.add_argument("--scheduled-countries", type=int, default=0)
    p_discover_europe.add_argument("--execute", action="store_true")

    p_discovery_status = sub.add_parser("discovery-status")
    p_discovery_status.add_argument("--countries-file", default="examples/countries.txt")

    p_scrape = sub.add_parser("scrape")
    p_scrape.add_argument("--limit", type=int, default=25)
    p_scrape.add_argument("--retry-failed", action="store_true")
    p_scrape.add_argument("--retry-no-email", action="store_true")
    p_scrape.add_argument("--max-seconds", type=float, default=0)
    p_scrape.add_argument("--fetch-attempts", type=int, default=32)
    p_scrape.add_argument("--fetch-timeout", type=float, default=5)
    p_scrape.add_argument("--site-max-seconds", type=float, default=0)
    p_scrape.add_argument("--workers", type=int, default=1)

    p_debug_scrape = sub.add_parser("debug-scrape-url")
    p_debug_scrape.add_argument("url")
    p_debug_scrape.add_argument("--fetch-timeout", type=float, default=5)
    p_debug_scrape.add_argument("--fetch-attempts", type=int, default=32)
    p_debug_scrape.add_argument("--site-max-seconds", type=float, default=60)

    p_rescrape_url = sub.add_parser("rescrape-url")
    p_rescrape_url.add_argument("url")
    p_rescrape_url.add_argument("--fetch-timeout", type=float, default=5)
    p_rescrape_url.add_argument("--fetch-attempts", type=int, default=32)
    p_rescrape_url.add_argument("--site-max-seconds", type=float, default=60)

    p_score = sub.add_parser("score")
    p_score.add_argument("--rescore", action="store_true")
    sub.add_parser("list-leads")
    sub.add_parser("stats")

    p_show_lead = sub.add_parser("show-lead")
    p_show_lead.add_argument("lead_id", type=int)

    p_draft = sub.add_parser("draft")
    p_draft.add_argument("--limit", type=int, default=25)
    p_draft.add_argument("--min-score", type=int, default=0)
    p_draft.add_argument("--include-personal", action="store_true")
    p_draft.add_argument("--replace-existing", action="store_true")
    p_draft.add_argument("--no-cache", action="store_true")

    p_manual_draft = sub.add_parser("manual-email-draft")
    p_manual_draft.add_argument("email")
    p_manual_draft.add_argument("--company", default="")
    p_manual_draft.add_argument("--website", default="")
    p_manual_draft.add_argument("--replace-existing", action="store_true")

    sub.add_parser("test-llm")
    sub.add_parser("test-smtp")
    sub.add_parser("list-drafts")

    p_show = sub.add_parser("show-draft")
    p_show.add_argument("draft_id", type=int)

    p_regenerate = sub.add_parser("regenerate-draft")
    p_regenerate.add_argument("draft_id", type=int)
    p_regenerate.add_argument("--no-cache", action="store_true")

    p_regenerate_unsent = sub.add_parser("regenerate-unsent-drafts")
    p_regenerate_unsent.add_argument("--no-cache", action="store_true")

    p_approve = sub.add_parser("approve-draft")
    p_approve.add_argument("draft_id", type=int)

    p_unapprove = sub.add_parser("unapprove-draft")
    p_unapprove.add_argument("draft_id", type=int)

    p_approve_batch = sub.add_parser("approve-batch")
    p_approve_batch.add_argument("--limit", type=int, default=20)
    p_approve_batch.add_argument("--min-score", type=int, default=8)
    p_approve_batch.add_argument("--include-personal", action="store_true")
    p_approve_batch.add_argument("--execute", action="store_true")

    p_export = sub.add_parser("export-csv")
    p_export.add_argument("csv_path")

    p_export_leads = sub.add_parser("export-leads-csv")
    p_export_leads.add_argument("csv_path")

    p_suppress = sub.add_parser("suppress")
    p_suppress.add_argument("email")
    p_suppress.add_argument("--reason", default="manual")

    p_send = sub.add_parser("send")
    p_send.add_argument("--limit", type=int, default=5)
    p_send.add_argument("--delay-seconds", type=float)
    p_send.add_argument("--dry-run", action="store_true")
    p_send.add_argument("--continue-after-sleep", action="store_true")

    p_send_draft = sub.add_parser("send-draft")
    p_send_draft.add_argument("draft_id", type=int)
    p_send_draft.add_argument("--dry-run", action="store_true")

    p_send_test = sub.add_parser("send-test")
    p_send_test.add_argument("draft_id", type=int)
    p_send_test.add_argument("recipient")
    p_send_test.add_argument("--dry-run", action="store_true")

    p_auto = sub.add_parser("auto-pipeline")
    p_auto.add_argument("--queries-file", default="examples/discovery_queries.txt")
    p_auto.add_argument("--countries-file", default="examples/countries.txt")
    p_auto.add_argument("--discover-count", type=int, default=10)
    p_auto.add_argument("--limit-queries", type=int, default=1)
    p_auto.add_argument("--limit-countries", type=int, default=1)
    p_auto.add_argument("--least-searched-countries", type=int, default=0)
    p_auto.add_argument("--scheduled-countries", type=int, default=3)
    p_auto.add_argument("--skip-discovery", action="store_true")
    p_auto.add_argument("--scrape-limit", type=int, default=100)
    p_auto.add_argument("--scrape-workers", type=int, default=4)
    p_auto.add_argument("--scrape-max-seconds", type=float, default=900)
    p_auto.add_argument("--fetch-attempts", type=int, default=28)
    p_auto.add_argument("--fetch-timeout", type=float, default=4)
    p_auto.add_argument("--site-max-seconds", type=float, default=60)
    p_auto.add_argument("--draft-limit", type=int, default=100)
    p_auto.add_argument("--draft-min-score", type=int, default=0)
    p_auto.add_argument("--include-personal", action="store_true")
    p_auto.add_argument("--no-cache", action="store_true")
    p_auto.add_argument("--no-auto-approve", action="store_true")
    p_auto.add_argument("--approve-limit", type=int, default=100)
    p_auto.add_argument("--approve-min-score", type=int, default=0)
    p_auto.add_argument("--send-limit", type=int, default=5)
    p_auto.add_argument(
        "--backlog-pause-threshold", type=int, default=100,
        help="Pause discovery and processing when this many approved unsent drafts exist; 0 disables pausing.",
    )
    p_auto.add_argument("--delay-seconds", type=float)
    p_auto.add_argument("--dry-run", action="store_true")
    p_auto.add_argument("--cycles", type=int, default=1, help="Number of cycles to run; 0 means run forever.")
    p_auto.add_argument("--sleep-seconds", type=float, default=0)
    p_auto.add_argument("--idle-sleep-seconds", type=float)
    p_auto.add_argument("--discovery-interval-seconds", type=float)
    p_auto.add_argument("--processing-interval-seconds", type=float)

    args = parser.parse_args(argv)
    settings = get_settings()
    conn = connect(settings.db_path)
    init_db(conn)

    if args.cmd == "init-db":
        print(f"Initialized {settings.db_path}")
        return 0

    if args.cmd == "discovery-status":
        countries = read_list_file(args.countries_file)
        status = country_discovery_status(conn, settings, countries)
        for country in countries:
            item = status[country]
            cooldown = f" cooling_until={item['cooldown_until']}" if item["cooldown_active"] else ""
            print(
                f"{country}: leads={item['lead_count']}/{item['target_count']} tier={item['tier']} "
                f"round={item['rounds_completed']} no_new={item['no_new_rounds']} "
                f"last={item['last_searched_at'] or '-'}{cooldown}"
            )
        return 0

    if args.cmd == "auto-pipeline":
        conn.close()
        return run_independent_auto_pipeline(args, settings)

    if args.cmd == "__legacy-auto-pipeline":
        auto_approve = not args.no_auto_approve
        send_real = not args.dry_run
        print("Auto pipeline started.")
        if send_real:
            print("Real sending is ENABLED by default for auto-pipeline.")
        else:
            print("Real sending is disabled because --dry-run was provided.")
        if auto_approve:
            print("Auto approval is ENABLED by default for auto-pipeline.")
        else:
            print("Auto approval is disabled because --no-auto-approve was provided.")

        cycle = 0
        delay = settings.send_delay_seconds if args.delay_seconds is None else args.delay_seconds
        while True:
            cycle += 1
            sent_this_cycle = False
            status = draft_send_status(conn)
            print("")
            print(
                f"--- Auto cycle {cycle}: drafts total={status['total']} "
                f"sent={status['sent']} unsent={status['unsent']} "
                f"approved_unsent={status['approved_unsent']} pending_unsent={status['pending_unsent']} "
                f"valid_unsent={status['valid_unsent']} ---",
                flush=True,
            )

            if status["valid_approved_unsent"] > 0:
                send_args = ["send", "--limit", str(args.send_limit)]
                if args.delay_seconds is not None:
                    send_args.extend(["--delay-seconds", str(args.delay_seconds)])
                send_args.append("--continue-after-sleep")
                if not send_real:
                    send_args.append("--dry-run")
                sent_before = status["sent"]
                rc = run_cli_step("send approved drafts", send_args)
                if rc:
                    return rc
                sent_this_cycle = draft_send_status(conn)["sent"] > sent_before
            elif status["valid_pending_unsent"] > 0 and not auto_approve:
                print(
                    "Pending unsent drafts exist. Review/approve them, or rerun auto-pipeline without --no-auto-approve.",
                    flush=True,
                )
            else:
                if status["valid_unsent"] == 0:
                    if status["unsent"] == 0:
                        print("All existing drafts are sent. Starting the next discovery/scrape/draft batch.", flush=True)
                    else:
                        print(
                            "Only invalid unsent drafts remain. Ignoring them and starting the next discovery/scrape/draft batch.",
                            flush=True,
                        )
                elif auto_approve:
                    print("No approved unsent drafts remain. Auto-approving eligible pending drafts first.", flush=True)

                if status["valid_unsent"] == 0:
                    if not args.skip_discovery:
                        discover_args = [
                            "discover-batch",
                            "--queries-file",
                            args.queries_file,
                            "--countries-file",
                            args.countries_file,
                            "--count",
                            str(args.discover_count),
                            "--limit-queries",
                            str(args.limit_queries),
                            "--limit-countries",
                            str(args.limit_countries),
                            "--execute",
                        ]
                        if args.least_searched_countries:
                            discover_args.extend(["--least-searched-countries", str(args.least_searched_countries)])
                        if args.scheduled_countries:
                            discover_args.extend(["--scheduled-countries", str(args.scheduled_countries)])
                        rc = run_cli_step("discover companies", discover_args)
                        if rc:
                            return rc

                    scrape_args = [
                        "scrape",
                        "--limit",
                        str(args.scrape_limit),
                        "--retry-failed",
                        "--max-seconds",
                        str(args.scrape_max_seconds),
                        "--fetch-attempts",
                        str(args.fetch_attempts),
                        "--fetch-timeout",
                        str(args.fetch_timeout),
                        "--site-max-seconds",
                        str(args.site_max_seconds),
                        "--workers",
                        str(args.scrape_workers),
                    ]
                    rc = run_cli_step("scrape emails", scrape_args)
                    if rc:
                        return rc
                    rc = run_cli_step("score leads", ["score"])
                    if rc:
                        return rc
                    draft_args = [
                        "draft",
                        "--limit",
                        str(args.draft_limit),
                        "--min-score",
                        str(args.draft_min_score),
                    ]
                    if args.include_personal:
                        draft_args.append("--include-personal")
                    if args.no_cache:
                        draft_args.append("--no-cache")
                    rc = run_cli_step("generate drafts", draft_args)
                    if rc:
                        return rc

                if auto_approve:
                    approve_args = [
                        "approve-batch",
                        "--limit",
                        str(args.approve_limit),
                        "--min-score",
                        str(args.approve_min_score),
                        "--execute",
                    ]
                    if args.include_personal:
                        approve_args.append("--include-personal")
                    rc = run_cli_step("approve drafts", approve_args)
                    if rc:
                        return rc
                    post_approve_status = draft_send_status(conn)
                    if post_approve_status["valid_approved_unsent"] <= 0:
                        print(
                            "No approved unsent drafts remain after approval. "
                            "Auto pipeline will retry discovery/scrape/draft in the next cycle.",
                            flush=True,
                        )
                    else:
                        send_args = ["send", "--limit", str(args.send_limit)]
                        if args.delay_seconds is not None:
                            send_args.extend(["--delay-seconds", str(args.delay_seconds)])
                        send_args.append("--continue-after-sleep")
                        if not send_real:
                            send_args.append("--dry-run")
                        sent_before = post_approve_status["sent"]
                        rc = run_cli_step("send approved drafts", send_args)
                        if rc:
                            return rc
                        sent_this_cycle = draft_send_status(conn)["sent"] > sent_before
                else:
                    print("Auto pipeline paused before approval because --no-auto-approve was provided.", flush=True)

            if args.cycles and cycle >= args.cycles:
                break
            next_status = draft_send_status(conn)
            if send_real and sent_this_cycle and delay > 0 and has_sendable_drafts(next_status, auto_approve):
                print(
                    f"Next auto cycle still has sendable drafts "
                    f"(approved={next_status['valid_approved_unsent']}, pending={next_status['valid_pending_unsent']}).",
                    flush=True,
                )
                if not sleep_with_progress(delay, continue_after_sleep=True, reason="next auto cycle"):
                    print("Stopping auto pipeline because the cycle wait interval was interrupted or suspended.", flush=True)
                    break
            elif not sent_this_cycle and args.idle_sleep_seconds > 0:
                print(
                    f"No emails were sent in this auto cycle. Sleeping {args.idle_sleep_seconds:g}s before retrying.",
                    flush=True,
                )
                time.sleep(args.idle_sleep_seconds)
            if args.sleep_seconds > 0:
                print(f"Sleeping {args.sleep_seconds:g}s before the next auto cycle.", flush=True)
                time.sleep(args.sleep_seconds)

        print("Auto pipeline finished.")
        return 0

    if args.cmd == "import-seeds":
        with open(args.csv_path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        count = upsert_leads(conn, rows)
        print(f"Imported {count} seed leads")
        return 0

    if args.cmd == "import-urls":
        rows = []
        for raw_line in Path(args.path).read_text(encoding="utf-8").splitlines():
            url = raw_line.strip()
            if not url or url.startswith("#"):
                continue
            rows.append({"company_name": "", "website": url, "source": args.source})
        count = upsert_leads(conn, rows)
        print(f"Imported {count} URLs")
        return 0

    if args.cmd == "import-leads-csv":
        with open(args.csv_path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        imported = import_leads_csv(conn, rows, args.source)
        print(f"Imported {imported} leads from {args.csv_path}")
        return 0

    if args.cmd == "discover":
        if args.state:
            queries = [(f"{query} {state}", state) for query in args.query for state in args.state]
        else:
            queries = [(query, "") for query in args.query]
        all_rows: list[dict] = []
        failed_queries = []
        known_websites = {
            normalize_url(row["website"])
            for row in conn.execute("SELECT website FROM leads")
            if normalize_url(row["website"])
        }
        run_records = []
        for query, country in queries:
            print(f"Searching: {query}")
            try:
                rows = discover_websites(settings, query, args.count)
            except Exception as exc:
                failed_queries.append((query, str(exc)))
                print(f"  failed: {exc}")
                run_records.append((query, country, 0, 0, 0, str(exc)))
                continue
            if country:
                for row in rows:
                    row["country"] = country
            all_rows.extend(rows)
            websites = {normalize_url(row.get("website", "")) for row in rows}
            websites.discard("")
            new_websites = websites - known_websites
            known_websites.update(websites)
            run_records.append((query, country, len(rows), len(websites), len(new_websites), ""))
            for row in rows:
                print(f"  {row['company_name']} -> {row['website']}")
        unique_rows = dedupe_rows(all_rows)
        print_discovery_summary(conn, all_rows, unique_rows)
        if failed_queries:
            print(f"Discovery warnings: {len(failed_queries)} queries failed and were skipped.")
            for query, error in failed_queries[:10]:
                print(f"  - {query}: {error}")
        if not args.execute:
            print(f"Preview only: {len(unique_rows)} unique websites. Re-run with --execute to import.")
            return 0
        result = import_discovered_rows(conn, unique_rows)
        for query, country, result_count, unique_count, new_count, error in run_records:
            record_discovery_run(
                conn,
                country=country,
                query=query,
                base_query=query,
                phase="manual",
                result_count=result_count,
                unique_count=unique_count,
                new_count=new_count,
                error=error,
            )
        conn.commit()
        print(
            f"Imported {result['processed']} discovered websites "
            f"({result['created']} new, {result['updated']} existing)"
        )
        return 0

    if args.cmd in {"discover-batch", "discover-europe"}:
        query_specs = parse_discovery_query_specs(read_list_file(args.queries_file))
        countries = read_list_file(args.countries_file)
        if args.offset_queries:
            query_specs = query_specs[max(args.offset_queries, 0) :]
        if args.offset_countries:
            countries = countries[max(args.offset_countries, 0) :]
        schedule_plan = None
        scheduled_by_country = {}
        if args.scheduled_countries:
            schedule_plan = plan_country_schedule(
                conn,
                settings,
                countries,
                args.scheduled_countries,
                args.limit_queries if args.limit_queries is not None else 3,
            )
            scheduled_by_country = {item.country: item for item in schedule_plan.countries}
            countries = [item.country for item in schedule_plan.countries]
            query_jobs = [
                row
                for item in schedule_plan.countries
                for row in scheduled_queries(conn, query_specs, item, settings)
            ]
            print(f"Fair discovery schedule: {len(countries)} country/countries, {len(query_jobs)} new query/query combinations.")
            for item in schedule_plan.countries:
                print(
                    f"  - {item.country}: round {item.round_no} {item.phase}, "
                    f"leads {item.lead_count}/{item.target_count}, query limit {item.query_limit}"
                )
            if schedule_plan.cooldown_skips:
                print(
                    "Cooldown skips this round: "
                    + ", ".join(country for country, _, _, _ in schedule_plan.cooldown_skips)
                )
            if not query_jobs:
                if schedule_plan.next_cooldown_at:
                    print(f"No country is ready now. Earliest cooldown ends at {schedule_plan.next_cooldown_at} UTC.")
                else:
                    print("No unused scheduled queries are available for the selected countries.")
                if args.execute and schedule_plan.cooldown_skips:
                    record_cooldown_skips(conn, schedule_plan.cooldown_skips)
                    conn.commit()
                return 0
        else:
            if args.least_searched_countries:
                countries = least_searched_countries(conn, countries, args.least_searched_countries)
            if args.limit_countries is not None:
                countries = countries[: args.limit_countries]
            if args.limit_queries is not None:
                query_specs = limit_query_specs_for_countries(query_specs, countries, args.limit_queries)
            print(
                f"Discovery plan: {len(query_specs)} query pattern(s), {len(countries)} country/countries."
            )
            if args.least_searched_countries and countries:
                print("Least-covered countries selected: " + ", ".join(countries))
            query_jobs = [
                {
                    "query": query,
                    "country": country,
                    "base_query": query.rsplit(f" {country}", 1)[0] if country else query,
                    "city": "",
                    "round_no": 0,
                    "phase": "manual",
                }
                for query, country in build_country_queries(query_specs, countries)
            ]
        all_rows = []
        failed_queries = []
        known_websites = {
            normalize_url(row["website"])
            for row in conn.execute("SELECT website FROM leads")
            if normalize_url(row["website"])
        }
        run_records = []
        round_totals = {
            country: {"queries": 0, "results": 0, "new": 0, "failed": 0}
            for country in scheduled_by_country
        }
        for index, job in enumerate(query_jobs, 1):
            query = job["query"]
            country = job["country"]
            print(f"[{index}/{len(query_jobs)}] Searching: {query}")
            try:
                rows = discover_websites(settings, query, args.count)
            except Exception as exc:
                failed_queries.append((query, str(exc)))
                print(f"  failed: {exc}")
                run_records.append({**job, "result_count": 0, "unique_count": 0, "new_count": 0, "error": str(exc)})
                if country in round_totals:
                    round_totals[country]["queries"] += 1
                    round_totals[country]["failed"] += 1
                continue
            for row in rows:
                row["country"] = country
            websites = {normalize_url(row.get("website", "")) for row in rows}
            websites.discard("")
            new_websites = websites - known_websites
            known_websites.update(websites)
            all_rows.extend(rows)
            print(f"  found {len(rows)} websites")
            print(f"  new to this database: {len(new_websites)}")
            for row in rows[:5]:
                print(f"  - {row['company_name']} -> {row['website']}")
            run_records.append(
                {
                    **job,
                    "result_count": len(rows),
                    "unique_count": len(websites),
                    "new_count": len(new_websites),
                    "error": "",
                }
            )
            if country in round_totals:
                round_totals[country]["queries"] += 1
                round_totals[country]["results"] += len(rows)
                round_totals[country]["new"] += len(new_websites)
        unique_rows = dedupe_rows(all_rows)
        print_discovery_summary(conn, all_rows, unique_rows)
        if failed_queries:
            print(f"Discovery warnings: {len(failed_queries)} queries failed and were skipped.")
            for query, error in failed_queries[:10]:
                print(f"  - {query}: {error}")
        if not args.execute:
            print(f"Preview only: {len(unique_rows)} unique websites. Re-run with --execute to import.")
            return 0
        result = import_discovered_rows(conn, unique_rows)
        for record in run_records:
            record_discovery_run(
                conn,
                country=record["country"],
                query=record["query"],
                base_query=record.get("base_query", ""),
                city=record.get("city", ""),
                round_no=int(record.get("round_no", 0)),
                phase=record.get("phase", "manual"),
                result_count=record["result_count"],
                unique_count=record["unique_count"],
                new_count=record["new_count"],
                error=record["error"],
            )
        if schedule_plan:
            record_cooldown_skips(conn, schedule_plan.cooldown_skips)
            for country, totals in round_totals.items():
                record_country_round(
                    conn,
                    scheduled_by_country[country],
                    query_count=totals["queries"],
                    result_count=totals["results"],
                    new_count=totals["new"],
                    failed_count=totals["failed"],
                )
        conn.commit()
        print(
            f"Imported {result['processed']} discovered websites "
            f"({result['created']} new, {result['updated']} existing)"
        )
        return 0

    if args.cmd == "scrape":
        if args.retry_no_email:
            rows = conn.execute(
                """
                SELECT id, website, emails, phones FROM leads
                WHERE emails IS NULL OR emails = '' OR emails = '[]'
                ORDER BY score DESC, id ASC
                LIMIT ?
                """,
                (args.limit,),
            ).fetchall()
            mode = "leads without emails"
        else:
            statuses = ("new", "scrape_failed") if args.retry_failed else ("new",)
            placeholders = ",".join("?" for _ in statuses)
            order_by = "CASE WHEN status = 'new' THEN 0 ELSE 1 END, id ASC" if args.retry_failed else "id ASC"
            rows = conn.execute(
                f"""
                SELECT id, website, emails, phones FROM leads
                WHERE status IN ({placeholders})
                ORDER BY {order_by}
                LIMIT ?
                """,
                (*statuses, args.limit),
            ).fetchall()
            mode = "new or failed" if args.retry_failed else "new"
        if not rows:
            print(f"No {mode} to scrape.")
            return 0
        if not args.max_seconds:
            print(f"Scrape has no overall time budget; processing up to {len(rows)} {mode}.", flush=True)
        started_at = time.monotonic()
        scraped_count = 0
        email_count = 0
        no_email_count = 0
        failed_count = 0

        def scrape_row(row) -> tuple:
            data = scrape_site(
                row["website"],
                max_fetch_attempts=args.fetch_attempts,
                fetch_timeout=args.fetch_timeout,
                max_elapsed_seconds=args.site_max_seconds,
            )
            return row, data

        def save_success(row, data) -> tuple[bool, int]:
            existing_emails = json.loads(row["emails"] or "[]")
            existing_phones = json.loads(row["phones"] or "[]")
            merged_emails = clean_emails_for_website(existing_emails + data["emails"], row["website"])
            merged_phones = sorted(set(existing_phones + data["phones"]))
            next_status = "scraped" if merged_emails else "no_email"
            note = "" if merged_emails else "No email found; scraped page text/phones only"
            conn.execute(
                """
                UPDATE leads
                SET status = ?, page_text = ?, emails = ?, phones = ?, llm_note = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    next_status,
                    data["page_text"],
                    json.dumps(merged_emails),
                    json.dumps(merged_phones),
                    note,
                    row["id"],
                ),
            )
            conn.commit()
            return bool(merged_emails), len(merged_emails)

        def save_failure(row, exc: Exception) -> None:
            conn.execute(
                "UPDATE leads SET status = 'scrape_failed', llm_note = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (str(exc)[:500], row["id"]),
            )
            conn.commit()

        workers = max(1, min(args.workers, len(rows)))
        if workers == 1:
            for row in rows:
                if args.max_seconds and time.monotonic() - started_at >= args.max_seconds:
                    print(f"Stopping scrape after {args.max_seconds:g}s time budget.", flush=True)
                    break
                print(f"Scraping {row['website']}", flush=True)
                try:
                    _, data = scrape_row(row)
                    has_email, email_total = save_success(row, data)
                    scraped_count += 1
                    if has_email:
                        email_count += 1
                        print(f"  found {email_total} email(s)", flush=True)
                    else:
                        no_email_count += 1
                        print("  no email found", flush=True)
                except Exception as exc:
                    save_failure(row, exc)
                    failed_count += 1
        else:
            print(f"Scraping {len(rows)} leads with {workers} workers", flush=True)
            row_iter = iter(rows)
            future_map = {}
            with ThreadPoolExecutor(max_workers=workers) as executor:
                def submit_next() -> bool:
                    if args.max_seconds and time.monotonic() - started_at >= args.max_seconds:
                        return False
                    try:
                        next_row = next(row_iter)
                    except StopIteration:
                        return False
                    future_map[executor.submit(scrape_row, next_row)] = next_row
                    return True

                for _ in range(workers):
                    if not submit_next():
                        break
                while future_map:
                    if args.max_seconds and time.monotonic() - started_at >= args.max_seconds:
                        print(f"Stopping scrape after {args.max_seconds:g}s time budget.", flush=True)
                        for future in future_map:
                            future.cancel()
                        break
                    done, _ = wait(future_map, timeout=1, return_when=FIRST_COMPLETED)
                    if not done:
                        continue
                    for future in done:
                        row = future_map.pop(future)
                        print(f"Scraping {row['website']}", flush=True)
                        try:
                            _, data = future.result()
                            has_email, email_total = save_success(row, data)
                            scraped_count += 1
                            if has_email:
                                email_count += 1
                                print(f"  found {email_total} email(s)", flush=True)
                            else:
                                no_email_count += 1
                                print("  no email found", flush=True)
                        except Exception as exc:
                            save_failure(row, exc)
                            failed_count += 1
                    while len(future_map) < workers and submit_next():
                        pass
        processed_count = scraped_count + failed_count
        print(
            f"Processed {processed_count} leads: "
            f"with emails {email_count}, no email {no_email_count}, failed {failed_count}"
        )
        return 0

    if args.cmd == "debug-scrape-url":
        try:
            data = scrape_site(
                args.url,
                max_fetch_attempts=args.fetch_attempts,
                fetch_timeout=args.fetch_timeout,
                max_elapsed_seconds=args.site_max_seconds,
            )
        except Exception as exc:
            print(f"Scrape failed: {exc}")
            return 1
        print("Emails:")
        for email in data["emails"]:
            print(f"  {email}")
        print("Phones:")
        for phone in data["phones"]:
            print(f"  {phone}")
        print("Text preview:")
        print(data["page_text"][:2000])
        return 0

    if args.cmd == "rescrape-url":
        website = normalize_url(args.url)
        row = conn.execute("SELECT id, emails, phones FROM leads WHERE website = ?", (website,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO leads (company_name, website, source, status) VALUES (?, ?, ?, 'new')",
                ("", website, "manual-rescrape"),
            )
            conn.commit()
            row = conn.execute("SELECT id, emails, phones FROM leads WHERE website = ?", (website,)).fetchone()
        try:
            data = scrape_site(
                website,
                max_fetch_attempts=args.fetch_attempts,
                fetch_timeout=args.fetch_timeout,
                max_elapsed_seconds=args.site_max_seconds,
            )
        except Exception as exc:
            conn.execute(
                "UPDATE leads SET status = 'scrape_failed', llm_note = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (str(exc)[:500], row["id"]),
            )
            conn.commit()
            print(f"Scrape failed for {website}: {exc}")
            return 1
        existing_emails = json.loads(row["emails"] or "[]")
        existing_phones = json.loads(row["phones"] or "[]")
        merged_emails = clean_emails_for_website(existing_emails + data["emails"], website)
        merged_phones = sorted(set(existing_phones + data["phones"]))
        conn.execute(
            """
            UPDATE leads
            SET status = 'scraped', page_text = ?, emails = ?, phones = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (data["page_text"], json.dumps(merged_emails), json.dumps(merged_phones), row["id"]),
        )
        conn.commit()
        print(f"Rescraped {website}: {len(merged_emails)} emails, {len(merged_phones)} phones")
        for email in merged_emails:
            print(f"  {email}")
        return 0

    if args.cmd == "score":
        statuses = ("scraped", "no_email", "scored", "drafted") if args.rescore else ("scraped", "no_email")
        placeholders = ",".join("?" for _ in statuses)
        rows = conn.execute(
            f"SELECT id, website, page_text, emails FROM leads WHERE status IN ({placeholders})",
            statuses,
        ).fetchall()
        for index, row in enumerate(rows, 1):
            emails = clean_emails_for_website(json.loads(row["emails"] or "[]"), row["website"])
            score, note = score_lead(row["page_text"] or "", emails)
            conn.execute(
                """
                UPDATE leads
                SET status = CASE WHEN status = 'scraped' THEN 'scored' ELSE status END,
                    score = ?,
                    emails = ?,
                    llm_note = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (score, json.dumps(emails), note, row["id"]),
            )
            if index % 100 == 0:
                conn.commit()
        conn.commit()
        print(f"Scored {len(rows)} leads")
        return 0

    if args.cmd == "list-leads":
        rows = conn.execute(
            """
            SELECT id, company_name, website, status, score, emails, llm_note
            FROM leads
            ORDER BY id DESC LIMIT 100
            """
        ).fetchall()
        for row in rows:
            email_count = len(json.loads(row["emails"] or "[]"))
            note = (row["llm_note"] or "")[:120]
            print(
                f"#{row['id']} status={row['status']} score={row['score']} emails={email_count} "
                f"company={row['company_name']} website={row['website']} note={note}"
            )
        return 0

    if args.cmd == "stats":
        print("Leads by status:")
        for row in conn.execute("SELECT status, COUNT(*) AS count FROM leads GROUP BY status ORDER BY status"):
            print(f"  {row['status']}: {row['count']}")
        total_leads = conn.execute("SELECT COUNT(*) AS count FROM leads").fetchone()["count"]
        with_email = conn.execute(
            "SELECT COUNT(*) AS count FROM leads WHERE emails IS NOT NULL AND emails != '[]'"
        ).fetchone()["count"]
        print(f"Total leads: {total_leads}")
        print(f"Leads with emails: {with_email}")

        print("Drafts:")
        draft_stats = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN approved = 1 THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent
            FROM email_drafts
            """
        ).fetchone()
        print(f"  total: {draft_stats['total'] or 0}")
        print(f"  approved: {draft_stats['approved'] or 0}")
        print(f"  sent: {draft_stats['sent'] or 0}")
        return 0

    if args.cmd == "show-lead":
        row = conn.execute(
            """
            SELECT id, company_name, website, source, status, score, emails, phones, llm_note
            FROM leads WHERE id = ?
            """,
            (args.lead_id,),
        ).fetchone()
        if not row:
            print(f"Lead {args.lead_id} not found")
            return 1
        emails = clean_emails_for_website(json.loads(row["emails"] or "[]"), row["website"])
        phones = json.loads(row["phones"] or "[]")
        print(f"Lead ID: {row['id']}")
        print(f"Company: {row['company_name']}")
        print(f"Website: {row['website']}")
        print(f"Source: {row['source']}")
        print(f"Status: {row['status']}")
        print(f"Score: {row['score']}")
        print(f"Note: {row['llm_note'] or ''}")
        print("Emails:")
        for email in emails:
            marker = " <- selected" if email == pick_recipient(emails) else ""
            print(f"  {email}{marker}")
        print("Phones:")
        for phone in phones:
            print(f"  {phone}")
        return 0

    if args.cmd == "draft":
        limit_clause = ""
        params: list[object] = [args.min_score]
        if args.limit and args.limit > 0:
            limit_clause = "LIMIT ?"
            params.append(args.limit)
        draft_filter = """
              AND NOT EXISTS (
                SELECT 1 FROM email_drafts sent_d
                WHERE sent_d.lead_id = leads.id AND sent_d.sent_at IS NOT NULL
              )
            """
        if not args.replace_existing:
            draft_filter += """
              AND NOT EXISTS (
                SELECT 1 FROM email_drafts d
                WHERE d.lead_id = leads.id
              )
            """
        rows = conn.execute(
            f"""
            SELECT * FROM leads
            WHERE score >= ?
              AND emails IS NOT NULL
              AND emails != ''
              AND emails != '[]'
              AND status != 'sent'
            {draft_filter}
            ORDER BY score DESC
            {limit_clause}
            """,
            params,
        ).fetchall()
        if not args.limit or args.limit <= 0:
            print(f"Draft generation has no batch limit; processing {len(rows)} eligible leads.", flush=True)
        made = 0
        for row in rows:
            emails = clean_emails_for_website(json.loads(row["emails"] or "[]"), row["website"])
            recipient = pick_recipient(emails)
            if not recipient:
                continue
            if not args.include_personal and not is_generic_business_email(recipient):
                conn.execute(
                    "UPDATE leads SET llm_note = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    ("Skipped draft: selected recipient looks personal; use --include-personal to draft anyway.", row["id"]),
                )
                conn.commit()
                continue
            suppressed = conn.execute("SELECT 1 FROM suppression_list WHERE email = ?", (recipient,)).fetchone()
            if suppressed:
                continue
            opening = default_opening(row["company_name"], row["page_text"] or "")
            if settings.use_llm:
                prompt = personalization_prompt(
                    row["company_name"] or "",
                    row["page_text"] or "",
                    lead_target_market(row, settings),
                    settings.company_business_summary,
                )
                key = cache_key(settings.llm_provider, settings.ollama_model, settings.deepseek_model, prompt)
                cached = None if args.no_cache else conn.execute("SELECT value FROM llm_cache WHERE cache_key = ?", (key,)).fetchone()
                if cached:
                    opening = cached["value"]
                else:
                    try:
                        opening = generate_text(settings, prompt)
                        opening = sanitize_opening(opening, row["company_name"], row["page_text"] or "")
                        conn.execute(
                            "INSERT OR REPLACE INTO llm_cache (cache_key, value) VALUES (?, ?)",
                            (key, opening),
                        )
                    except Exception as exc:
                        print(f"LLM skipped for lead {row['id']}: {exc}", file=sys.stderr)

            subject = subject_for(row["company_name"])
            body = draft_body(settings, row["company_name"], opening, settings.mail_reply_to)
            if args.replace_existing:
                conn.execute(
                    "DELETE FROM email_drafts WHERE lead_id = ? AND approved = 0 AND sent_at IS NULL",
                    (row["id"],),
                )
            conn.execute(
                """
                INSERT INTO email_drafts (lead_id, recipient, subject, body)
                VALUES (?, ?, ?, ?)
                """,
                (row["id"], recipient, subject, body),
            )
            conn.execute("UPDATE leads SET status = 'drafted', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],))
            # Release the write lock before the next potentially slow LLM request.
            conn.commit()
            made += 1
        conn.commit()
        print(f"Created {made} drafts")
        return 0

    if args.cmd == "regenerate-draft":
        row = conn.execute(
            """
            SELECT d.id AS draft_id, d.recipient, d.sent_at,
                   l.id AS lead_id, l.company_name, l.page_text, l.country
            FROM email_drafts d JOIN leads l ON l.id = d.lead_id
            WHERE d.id = ?
            """,
            (args.draft_id,),
        ).fetchone()
        if not row:
            print(f"Draft {args.draft_id} not found")
            return 1
        if row["sent_at"]:
            print(f"Draft {args.draft_id} was already sent and cannot be regenerated")
            return 1
        opening = default_opening(row["company_name"], row["page_text"] or "")
        if settings.use_llm:
            prompt = personalization_prompt(
                row["company_name"] or "",
                row["page_text"] or "",
                lead_target_market(row, settings),
                settings.company_business_summary,
            )
            key = cache_key(settings.llm_provider, settings.ollama_model, settings.deepseek_model, prompt)
            cached = None if args.no_cache else conn.execute("SELECT value FROM llm_cache WHERE cache_key = ?", (key,)).fetchone()
            if cached:
                opening = cached["value"]
            else:
                try:
                    opening = generate_text(settings, prompt)
                    opening = sanitize_opening(opening, row["company_name"], row["page_text"] or "")
                    conn.execute("INSERT OR REPLACE INTO llm_cache (cache_key, value) VALUES (?, ?)", (key, opening))
                except Exception as exc:
                    print(f"LLM skipped for draft {args.draft_id}: {exc}", file=sys.stderr)
                    return 1
        subject = subject_for(row["company_name"])
        body = draft_body(settings, row["company_name"], opening, settings.mail_reply_to)
        result = conn.execute(
            """
            UPDATE email_drafts
            SET subject = ?, body = ?, approved = 0
            WHERE id = ? AND sent_at IS NULL
            """,
            (subject, body, args.draft_id),
        )
        conn.commit()
        if not result.rowcount:
            print(f"Draft {args.draft_id} was sent before regeneration finished")
            return 1
        print(f"Regenerated draft {args.draft_id} for {row['recipient']} and marked it pending")
        return 0

    if args.cmd == "regenerate-unsent-drafts":
        rows = conn.execute(
            """
            SELECT d.id AS draft_id, d.recipient, d.approved,
                   l.company_name, l.page_text, l.country
            FROM email_drafts d JOIN leads l ON l.id = d.lead_id
            WHERE d.sent_at IS NULL
            ORDER BY d.approved DESC, d.id DESC
            """
        ).fetchall()
        updated = 0
        approved_updated = 0
        llm_ok = 0
        llm_failed = 0
        for index, row in enumerate(rows, 1):
            opening = default_opening(row["company_name"], row["page_text"] or "")
            if settings.use_llm:
                prompt = personalization_prompt(
                    row["company_name"] or "",
                    row["page_text"] or "",
                    lead_target_market(row, settings),
                    settings.company_business_summary,
                )
                key = cache_key(settings.llm_provider, settings.ollama_model, settings.deepseek_model, prompt)
                cached = None if args.no_cache else conn.execute("SELECT value FROM llm_cache WHERE cache_key = ?", (key,)).fetchone()
                if cached:
                    opening = cached["value"]
                    llm_ok += 1
                else:
                    try:
                        opening = generate_text(settings, prompt)
                        opening = sanitize_opening(opening, row["company_name"], row["page_text"] or "")
                        conn.execute("INSERT OR REPLACE INTO llm_cache (cache_key, value) VALUES (?, ?)", (key, opening))
                        llm_ok += 1
                    except Exception as exc:
                        llm_failed += 1
                        print(f"LLM skipped for draft {row['draft_id']}: {exc}", file=sys.stderr)
                        if index % 10 == 0:
                            conn.commit()
                            print(f"Processed {index}/{len(rows)} unsent drafts; regenerated {updated} (previously approved: {approved_updated}); LLM failed: {llm_failed}", flush=True)
                        continue
            subject = subject_for(row["company_name"])
            body = draft_body(settings, row["company_name"], opening, settings.mail_reply_to)
            result = conn.execute(
                """
                UPDATE email_drafts
                SET subject = ?, body = ?, approved = 0
                WHERE id = ? AND sent_at IS NULL
                """,
                (subject, body, row["draft_id"]),
            )
            if result.rowcount:
                updated += 1
                approved_updated += int(row["approved"])
            if index % 10 == 0:
                conn.commit()
                print(f"Processed {index}/{len(rows)} unsent drafts; regenerated {updated} (previously approved: {approved_updated}); LLM failed: {llm_failed}", flush=True)
        conn.commit()
        print(f"Regenerated {updated} unsent drafts (previously approved: {approved_updated}); marked regenerated drafts pending")
        print(f"LLM ok: {llm_ok}; LLM failed: {llm_failed}")
        return 1 if llm_failed else 0

    if args.cmd == "manual-email-draft":
        email = args.email.lower().strip()
        if not re.fullmatch(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", email, flags=re.IGNORECASE):
            print(f"Invalid email: {args.email}")
            return 1
        domain = email.rsplit("@", 1)[1]
        website = normalize_url(args.website or domain)
        company_name = args.company.strip() or company_name_from_domain(domain)
        row = conn.execute("SELECT id, emails, phones FROM leads WHERE website = ?", (website,)).fetchone()
        if not row:
            conn.execute(
                """
                INSERT INTO leads (company_name, website, source, status, page_text, emails, phones, score, llm_note)
                VALUES (?, ?, 'manual-email', 'scored', ?, ?, '[]', 2, '+2 manually added email')
                """,
                (
                    company_name,
                    website,
                    f"Manual email lead for {company_name}.",
                    json.dumps([email]),
                ),
            )
            conn.commit()
            row = conn.execute("SELECT id, emails, phones FROM leads WHERE website = ?", (website,)).fetchone()
        else:
            existing_emails = json.loads(row["emails"] or "[]")
            merged_emails = clean_emails_for_website(existing_emails + [email], website)
            conn.execute(
                """
                UPDATE leads
                SET company_name = COALESCE(NULLIF(?, ''), company_name),
                    status = CASE WHEN status = 'sent' THEN status ELSE 'scored' END,
                    emails = ?,
                    score = CASE WHEN score < 2 THEN 2 ELSE score END,
                    llm_note = CASE
                        WHEN llm_note IS NULL OR llm_note = '' THEN '+2 manually added email'
                        ELSE llm_note
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (company_name, json.dumps(merged_emails), row["id"]),
            )
            conn.commit()
        lead = conn.execute("SELECT * FROM leads WHERE website = ?", (website,)).fetchone()
        if args.replace_existing:
            conn.execute(
                """
                DELETE FROM email_drafts
                WHERE lead_id = ? AND lower(recipient) = lower(?) AND approved = 0 AND sent_at IS NULL
                """,
                (lead["id"], email),
            )
        existing_draft = conn.execute(
            """
            SELECT id, sent_at FROM email_drafts
            WHERE lead_id = ? AND lower(recipient) = lower(?)
            ORDER BY sent_at IS NOT NULL DESC, id DESC
            LIMIT 1
            """,
            (lead["id"], email),
        ).fetchone()
        if existing_draft and not args.replace_existing:
            print(f"Draft already exists for {email}: #{existing_draft['id']}")
            return 0
        if existing_draft and existing_draft["sent_at"]:
            print(f"Draft already sent for {email}: #{existing_draft['id']} at {existing_draft['sent_at']}")
            return 0
        subject = subject_for(lead["company_name"])
        opening = f"I am reaching out to introduce {settings.company_name} as a potential supplier for your business."
        body = draft_body(settings, lead["company_name"], opening, settings.mail_reply_to)
        conn.execute(
            """
            INSERT INTO email_drafts (lead_id, recipient, subject, body)
            VALUES (?, ?, ?, ?)
            """,
            (lead["id"], email, subject, body),
        )
        conn.execute("UPDATE leads SET status = 'drafted', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (lead["id"],))
        conn.commit()
        draft_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        print(f"Created manual draft #{draft_id} for {email} ({lead['company_name']})")
        return 0

    if args.cmd == "test-llm":
        if not settings.use_llm:
            print("USE_LLM is disabled. Set USE_LLM=1 in .env to test provider calls.")
            return 1
        prompt = personalization_prompt(
            "Example Distributor",
            "Example Distributor stocks industrial products and components for business buyers.",
            "Example target market",
            settings.company_business_summary,
        )
        response = generate_text(settings, prompt)
        print(response)
        return 0

    if args.cmd == "test-smtp":
        try:
            test_smtp(settings)
        except smtplib.SMTPAuthenticationError as exc:
            print(f"SMTP authentication failed: {exc.smtp_code} {exc.smtp_error.decode('utf-8', errors='ignore')}")
            return 1
        except TimeoutError:
            print(f"SMTP connection timed out: {settings.smtp_host}:{settings.smtp_port}")
            return 1
        except OSError as exc:
            print(f"SMTP connection failed: {exc}")
            return 1
        print(
            f"SMTP login OK for {settings.mail_from} via "
            f"{settings.smtp_host}:{settings.smtp_port} ({settings.smtp_security})"
        )
        return 0

    if args.cmd == "list-drafts":
        rows = conn.execute(
            """
            SELECT d.id, d.recipient, d.approved, d.sent_at, l.company_name, l.score, d.subject
            FROM email_drafts d JOIN leads l ON l.id = d.lead_id
            ORDER BY d.id DESC LIMIT 50
            """
        ).fetchall()
        for row in rows:
            print(
                f"#{row['id']} approved={row['approved']} sent={row['sent_at'] or '-'} "
                f"score={row['score']} to={row['recipient']} company={row['company_name']} subject={row['subject']}"
            )
        return 0

    if args.cmd == "show-draft":
        row = conn.execute(
            """
            SELECT d.id, d.recipient, d.subject, d.body, d.approved, d.sent_at,
                   l.company_name, l.website, l.score
            FROM email_drafts d JOIN leads l ON l.id = d.lead_id
            WHERE d.id = ?
            """,
            (args.draft_id,),
        ).fetchone()
        if not row:
            print(f"Draft {args.draft_id} not found")
            return 1
        print(f"Draft ID: {row['id']}")
        print(f"Company: {row['company_name']}")
        print(f"Website: {row['website']}")
        print(f"Score: {row['score']}")
        print(f"Approved: {row['approved']}")
        print(f"Sent at: {row['sent_at'] or '-'}")
        print(f"To: {row['recipient']}")
        print(f"Subject: {row['subject']}")
        print("")
        print(row["body"])
        return 0

    if args.cmd == "approve-draft":
        conn.execute("UPDATE email_drafts SET approved = 1 WHERE id = ?", (args.draft_id,))
        conn.commit()
        print(f"Approved draft {args.draft_id}")
        return 0

    if args.cmd == "unapprove-draft":
        row = conn.execute("SELECT sent_at FROM email_drafts WHERE id = ?", (args.draft_id,)).fetchone()
        if not row:
            print(f"Draft {args.draft_id} not found")
            return 1
        if row["sent_at"]:
            print(f"Draft {args.draft_id} was already sent and cannot be unapproved")
            return 1
        conn.execute("UPDATE email_drafts SET approved = 0 WHERE id = ?", (args.draft_id,))
        conn.commit()
        print(f"Unapproved draft {args.draft_id}")
        return 0

    if args.cmd == "approve-batch":
        email_filter = ""
        if not args.include_personal:
            email_filter = """
              AND (
                d.recipient LIKE 'sales@%' OR d.recipient LIKE 'vertrieb@%' OR
                d.recipient LIKE 'export@%' OR d.recipient LIKE 'info@%' OR
                d.recipient LIKE 'contact@%' OR d.recipient LIKE 'office@%' OR
                d.recipient LIKE 'service@%' OR d.recipient LIKE 'quotes@%' OR
                d.recipient LIKE 'quote%' OR d.recipient LIKE 'inquiry@%' OR
                d.recipient LIKE 'enquiry@%' OR d.recipient LIKE 'anfrage@%' OR
                d.recipient LIKE 'kontakt@%'
              )
            """
        candidate_rows = conn.execute(
            f"""
            SELECT d.id, d.recipient, d.subject, l.company_name, l.score
            FROM email_drafts d JOIN leads l ON l.id = d.lead_id
            WHERE d.approved = 0 AND d.sent_at IS NULL AND l.score >= ?
            {email_filter}
            ORDER BY l.score DESC, d.id
            """,
            (args.min_score,),
        ).fetchall()
        skipped_invalid = [row for row in candidate_rows if not is_valid_business_email(row["recipient"])]
        rows = [row for row in candidate_rows if is_valid_business_email(row["recipient"])][: args.limit]
        if skipped_invalid:
            print(f"Skipped {len(skipped_invalid)} drafts with invalid recipient emails.")
        if not rows:
            print("No matching drafts to approve")
            return 0
        for row in rows:
            print(
                f"#{row['id']} score={row['score']} to={row['recipient']} "
                f"company={row['company_name']} subject={row['subject']}"
            )
        if not args.execute:
            print("Preview only. Re-run with --execute to approve these drafts.")
            return 0
        conn.executemany("UPDATE email_drafts SET approved = 1 WHERE id = ?", [(row["id"],) for row in rows])
        conn.commit()
        print(f"Approved {len(rows)} drafts")
        return 0

    if args.cmd == "export-csv":
        rows = conn.execute(
            """
            SELECT d.id, l.company_name, l.website, l.country, l.score, l.emails, l.phones,
                   d.recipient, d.subject, d.body, d.approved, d.sent_at
            FROM email_drafts d JOIN leads l ON l.id = d.lead_id
            ORDER BY d.id
            """
        ).fetchall()
        Path(args.csv_path).parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "draft_id",
                    "company_name",
                    "website",
                    "country",
                    "score",
                    "all_emails",
                    "phones",
                    "recipient",
                    "subject",
                    "body",
                    "approved",
                    "sent_at",
                ]
            )
            for row in rows:
                all_emails = "; ".join(json.loads(row["emails"] or "[]"))
                phones = "; ".join(json.loads(row["phones"] or "[]"))
                writer.writerow(
                    [
                        row["id"],
                        row["company_name"],
                        row["website"],
                        row["country"],
                        row["score"],
                        all_emails,
                        phones,
                        row["recipient"],
                        row["subject"],
                        row["body"],
                        row["approved"],
                        row["sent_at"],
                    ]
                )
        print(f"Exported {len(rows)} drafts to {args.csv_path}")
        return 0

    if args.cmd == "export-leads-csv":
        rows = conn.execute(
            """
            SELECT id, company_name, website, source, country, status, score, emails, phones, llm_note
            FROM leads
            ORDER BY id
            """
        ).fetchall()
        Path(args.csv_path).parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "lead_id",
                    "company_name",
                    "website",
                    "source",
                    "country",
                    "status",
                    "score",
                    "emails",
                    "phones",
                    "note",
                ]
            )
            for row in rows:
                writer.writerow(
                    [
                        row["id"],
                        row["company_name"],
                        row["website"],
                        row["source"],
                        row["country"],
                        row["status"],
                        row["score"],
                        "; ".join(json.loads(row["emails"] or "[]")),
                        "; ".join(json.loads(row["phones"] or "[]")),
                        row["llm_note"],
                    ]
                )
        print(f"Exported {len(rows)} leads to {args.csv_path}")
        return 0

    if args.cmd == "suppress":
        conn.execute(
            "INSERT OR REPLACE INTO suppression_list (email, reason) VALUES (?, ?)",
            (args.email.lower(), args.reason),
        )
        conn.commit()
        print(f"Suppressed {args.email}")
        return 0

    if args.cmd == "send":
        delay = settings.send_delay_seconds if args.delay_seconds is None else args.delay_seconds
        today_sent, _ = global_sent_state(settings.db_path)
        remaining_quota = max(settings.send_daily_limit - today_sent, 0)
        if args.dry_run:
            effective_limit = args.limit
            print(
                f"DRY RUN only. Global daily send status: {today_sent}/{settings.send_daily_limit} sent today, "
                f"{remaining_quota} remaining. Configured delay: {delay:g}s."
            )
        else:
            if remaining_quota <= 0:
                print(f"Global daily send limit reached: {today_sent}/{settings.send_daily_limit} sent today across all tasks.")
                return 0
            effective_limit = min(args.limit, remaining_quota)
            print(
                f"Global daily send status: {today_sent}/{settings.send_daily_limit} sent today across all tasks, "
                f"sending up to {effective_limit} more with a shared {delay:g}s interval."
            )
        candidate_limit = max(effective_limit * 5, effective_limit + 25)
        rows = conn.execute(
            """
            SELECT * FROM email_drafts
            WHERE approved = 1 AND sent_at IS NULL
            ORDER BY id
            LIMIT ?
            """,
            (candidate_limit,),
        ).fetchall()
        sent = 0
        skipped_invalid = 0
        skipped_suppressed = 0
        for row in rows:
            recipient = row["recipient"].lower().strip()
            if not is_valid_business_email(recipient):
                skipped_invalid += 1
                print(f"Skipping draft #{row['id']}: invalid recipient {row['recipient']}", flush=True)
                if not args.dry_run:
                    conn.execute("UPDATE email_drafts SET approved = 0 WHERE id = ?", (row["id"],))
                    conn.commit()
                continue
            suppressed = conn.execute("SELECT 1 FROM suppression_list WHERE email = ?", (recipient,)).fetchone()
            if suppressed:
                skipped_suppressed += 1
                print(f"Skipping draft #{row['id']}: suppressed recipient {recipient}", flush=True)
                if not args.dry_run:
                    conn.execute("UPDATE email_drafts SET approved = 0 WHERE id = ?", (row["id"],))
                    conn.commit()
                continue
            if args.dry_run:
                print(f"DRY RUN send draft #{row['id']} to {recipient}: {row['subject']}")
                if settings.mail_cc:
                    print(f"  cc: {settings.mail_cc}")
                if settings.mail_bcc:
                    print(f"  bcc: {settings.mail_bcc}")
            else:
                send_slot, slot_message = acquire_send_slot(
                    settings.db_path,
                    delay,
                    settings.send_daily_limit,
                    continue_after_sleep=args.continue_after_sleep,
                )
                if not send_slot:
                    print(slot_message, flush=True)
                    break
                started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{started_at}] Sending draft #{row['id']} to {recipient}", flush=True)
                try:
                    send_email(settings, recipient, row["subject"], row["body"])
                    conn.execute("UPDATE email_drafts SET sent_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],))
                    conn.execute(
                        "UPDATE leads SET status = 'sent', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (row["lead_id"],),
                    )
                    conn.commit()
                except smtplib.SMTPNotSupportedError as exc:
                    if recipient_needs_smtputf8(recipient, settings):
                        conn.execute("UPDATE email_drafts SET approved = 0 WHERE id = ?", (row["id"],))
                        conn.commit()
                        send_slot.release()
                        print(
                            f"Skipping draft #{row['id']}: recipient requires SMTPUTF8, "
                            "which this SMTP server does not support. Draft preserved and unapproved.",
                            flush=True,
                        )
                        continue
                    failed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(f"[{failed_at}] SMTPUTF8 configuration error for draft #{row['id']}: {exc}", flush=True)
                    send_slot.release()
                    break
                except Exception as exc:
                    failed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"[{failed_at}] Send or local status update failed for draft #{row['id']} to {recipient}: {exc}",
                        flush=True,
                    )
                    print("Stopping send batch. Check this draft before retrying.", flush=True)
                    send_slot.release()
                    break
                send_slot.release()
                finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{finished_at}] Sent draft #{row['id']} to {recipient}", flush=True)
            sent += 1
            if sent >= effective_limit:
                break
            if not args.dry_run and delay > 0:
                if not sleep_with_progress(delay, continue_after_sleep=args.continue_after_sleep):
                    print("Stopping send batch because the wait interval was interrupted or suspended.", flush=True)
                    break
        conn.commit()
        if skipped_invalid or skipped_suppressed:
            print(
                f"Skipped {skipped_invalid} invalid and {skipped_suppressed} suppressed approved drafts.",
                flush=True,
            )
        print(f"Processed {sent} approved drafts")
        return 0

    if args.cmd == "send-draft":
        row = conn.execute("SELECT * FROM email_drafts WHERE id = ?", (args.draft_id,)).fetchone()
        if not row:
            print(f"Draft {args.draft_id} not found")
            return 1
        if row["sent_at"]:
            print(f"Draft {args.draft_id} was already sent at {row['sent_at']}")
            return 1
        if not row["approved"]:
            print(f"Draft {args.draft_id} is pending. Approve it before sending.")
            return 1
        recipient = row["recipient"].lower().strip()
        if not is_valid_business_email(recipient):
            print(f"Draft {args.draft_id} has invalid recipient: {row['recipient']}")
            conn.execute("UPDATE email_drafts SET approved = 0 WHERE id = ?", (row["id"],))
            conn.commit()
            return 1
        suppressed = conn.execute("SELECT 1 FROM suppression_list WHERE email = ?", (recipient,)).fetchone()
        if suppressed:
            print(f"Draft {args.draft_id} recipient is suppressed: {recipient}")
            return 1
        if args.dry_run:
            print(f"DRY RUN send draft #{row['id']} to {recipient}: {row['subject']}")
            if settings.mail_cc:
                print(f"  cc: {settings.mail_cc}")
            if settings.mail_bcc:
                print(f"  bcc: {settings.mail_bcc}")
            return 0
        today_sent, _ = global_sent_state(settings.db_path)
        if today_sent >= settings.send_daily_limit:
            print(f"Global daily send limit reached: {today_sent}/{settings.send_daily_limit} sent today across all tasks.")
            return 1
        send_slot, slot_message = acquire_send_slot(
            settings.db_path,
            settings.send_delay_seconds,
            settings.send_daily_limit,
        )
        if not send_slot:
            print(slot_message)
            return 1
        try:
            send_email(settings, recipient, row["subject"], row["body"])
            conn.execute("UPDATE email_drafts SET sent_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],))
            conn.execute(
                "UPDATE leads SET status = 'sent', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row["lead_id"],),
            )
            conn.commit()
        except Exception as exc:
            send_slot.release()
            print(f"Send or local status update failed for draft #{row['id']} to {recipient}: {exc}")
            return 1
        send_slot.release()
        print(f"Sent draft #{row['id']} to {recipient}")
        return 0

    if args.cmd == "send-test":
        row = conn.execute("SELECT * FROM email_drafts WHERE id = ?", (args.draft_id,)).fetchone()
        if not row:
            print(f"Draft {args.draft_id} not found")
            return 1
        test_recipient = args.recipient.lower().strip()
        if not is_valid_business_email(test_recipient):
            print(f"Invalid test recipient: {args.recipient}")
            return 1
        subject = "[TEST] " + row["subject"]
        body = (
            "This is a test copy of an outreach draft. The original recipient has not been emailed.\n\n"
            f"Original recipient: {row['recipient']}\n\n"
            + row["body"]
        )
        if args.dry_run:
            print(f"DRY RUN test send draft #{row['id']} to {test_recipient}: {subject}")
            if settings.mail_cc:
                print(f"cc: {settings.mail_cc}")
            if settings.mail_bcc:
                print(f"bcc: {settings.mail_bcc}")
            print("")
            print(body)
            return 0
        try:
            send_email(settings, test_recipient, subject, body)
        except Exception as exc:
            print(f"Test send failed for draft #{row['id']} to {test_recipient}: {exc}")
            return 1
        print(f"Sent test copy of draft #{row['id']} to {test_recipient}")
        return 0

    return 1


def default_opening(company_name: str | None, page_text: str) -> str:
    if "api 5l" in page_text.lower():
        return f"I noticed {company_name or 'your company'} references specialized industrial product requirements."
    if "carbon steel" in page_text.lower():
        return f"I noticed {company_name or 'your company'} works with industrial material sourcing."
    if "steel tubing" in page_text.lower():
        return f"I noticed {company_name or 'your company'} works with technical industrial products."
    return f"I saw {company_name or 'your company'} works in a relevant supply or distribution market."


def lead_target_market(row: sqlite3.Row, settings: Settings) -> str:
    try:
        country = (row["country"] or "").strip()
    except (KeyError, IndexError):
        country = ""
    return country or "not specified"


def company_name_from_domain(domain: str) -> str:
    stem = domain.split(".")[0]
    words = [part for part in re.split(r"[-_]+", stem) if part]
    return " ".join(word.capitalize() for word in words) or domain


def sanitize_opening(opening: str, company_name: str | None, page_text: str) -> str:
    clean = " ".join((opening or "").split()).strip()
    invalid_patterns = [
        r"\[[^\]]+\]",
        r"\byour product\b",
        r"\byour product/service\b",
        r"\bproduct/service\b",
        r"\bspecific category\b",
        r"\be\.g\.",
        r"\byour product category\b",
        r"\byour product type\b",
    ]
    if not clean or any(re.search(pattern, clean, re.IGNORECASE) for pattern in invalid_patterns):
        return default_opening(company_name, page_text)
    if "[" in clean or "]" in clean:
        return default_opening(company_name, page_text)
    return clean


def build_discovery_queries(base_queries: list[str], states: list[str] | None) -> list[str]:
    if not states:
        return base_queries
    return [f"{query} {state}" for query in base_queries for state in states]


def read_list_file(path: str) -> list[str]:
    values = []
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            values.append(line)
    return values


def parse_discovery_query_specs(lines: list[str]) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    for line in lines:
        if "::" in line:
            country, query = line.split("::", 1)
            clean_country = country.strip()
            clean_query = query.strip()
            if clean_query:
                specs.append((clean_query, clean_country))
        else:
            specs.append((line, ""))
    return specs


def build_country_queries(query_specs: list[tuple[str, str]], countries: list[str]) -> list[tuple[str, str]]:
    selected_countries = {country.lower(): country for country in countries}
    queries: list[tuple[str, str]] = []
    for base_query, specific_country in query_specs:
        if specific_country:
            country = selected_countries.get(specific_country.lower())
            if country:
                queries.append((f"{base_query} {country}", country))
            continue
        for country in countries:
            queries.append((f"{base_query} {country}", country))
    return queries


def limit_query_specs_for_countries(
    query_specs: list[tuple[str, str]], countries: list[str], limit: int
) -> list[tuple[str, str]]:
    selected_countries = {country.strip().lower() for country in countries}
    limited_specs: list[tuple[str, str]] = []
    global_count = 0
    global_limit = max(limit, 0)
    for query, country in query_specs:
        if country:
            if country.strip().lower() in selected_countries:
                limited_specs.append((query, country))
            continue
        if global_count < global_limit:
            limited_specs.append((query, country))
            global_count += 1
    return limited_specs


def dedupe_rows(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for row in rows:
        website = row.get("website", "").rstrip("/")
        if not website or website in seen:
            continue
        seen.add(website)
        unique.append(row)
    return unique


def print_discovery_summary(conn, all_rows: list[dict], unique_rows: list[dict]) -> None:
    existing = 0
    for row in unique_rows:
        website = normalize_url(row.get("website", ""))
        if website and conn.execute("SELECT 1 FROM leads WHERE website = ?", (website,)).fetchone():
            existing += 1
    new_count = len(unique_rows) - existing
    duplicate_count = max(len(all_rows) - len(unique_rows), 0)
    print(
        "Discovery summary: "
        f"{len(all_rows)} raw candidates, {len(unique_rows)} unique websites, "
        f"{duplicate_count} duplicate candidate(s), {existing} already in DB, {new_count} new."
    )


def wait_before_first_send(conn, delay: float, continue_after_sleep: bool = False) -> bool:
    row = conn.execute(
        "SELECT strftime('%s', MAX(sent_at)) AS ts FROM email_drafts WHERE sent_at IS NOT NULL"
    ).fetchone()
    if not row or row["ts"] is None:
        return True
    elapsed = time.time() - int(row["ts"])
    remaining = delay - elapsed
    if remaining <= 0:
        return True
    print(
        f"Last sent email was {elapsed:.0f}s ago. Waiting {remaining:.0f}s more before next email.",
        flush=True,
    )
    return sleep_with_progress(remaining, continue_after_sleep=continue_after_sleep)


def sleep_with_progress(delay: float, continue_after_sleep: bool = False, reason: str = "next email") -> bool:
    started_monotonic = time.monotonic()
    started_wall = time.time()
    deadline = started_monotonic + delay
    print(f"Waiting {delay:g}s before {reason}.", flush=True)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 60))
    monotonic_elapsed = time.monotonic() - started_monotonic
    wall_elapsed = time.time() - started_wall
    if wall_elapsed > delay + 30:
        print(
            f"Wait interrupted: wall-clock wait was {wall_elapsed:.0f}s; expected {delay:g}s. "
            "The computer or process was likely suspended.",
            flush=True,
        )
        if continue_after_sleep:
            print("Continuing send batch because --continue-after-sleep is enabled.", flush=True)
            return True
        return False
    if monotonic_elapsed > delay + 5:
        print(
            f"Wait finished after {monotonic_elapsed:.0f}s; expected {delay:g}s.",
            flush=True,
        )
    return True


def wait_gap_was_interrupted(conn, delay: float) -> bool:
    row = conn.execute(
        "SELECT strftime('%s', MAX(sent_at)) AS ts FROM email_drafts WHERE sent_at IS NOT NULL"
    ).fetchone()
    if not row or row["ts"] is None:
        return False
    elapsed = time.time() - int(row["ts"])
    if elapsed <= delay + 30:
        return False
    print(
        f"Wait interrupted: last sent email was {elapsed:.0f}s ago; expected about {delay:g}s.",
        flush=True,
    )
    return True


def least_searched_countries(conn, countries: list[str], limit: int) -> list[str]:
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
    indexed = list(enumerate(countries))
    indexed.sort(key=lambda item: (counts.get(item[1].strip().lower(), 0), item[0]))
    return [country for _, country in indexed[: max(limit, 0)]]


def import_leads_csv(conn, rows: list[dict], source: str) -> int:
    imported = 0
    for row in rows:
        normalized = {str(key or "").strip().lower(): value for key, value in row.items()}
        website = normalize_url(
            first_value(
                normalized,
                [
                    "website",
                    "websiteurl",
                    "website_url",
                    "site",
                    "url",
                    "company website",
                    "business website",
                ],
            )
        )
        if not website:
            continue
        company_name = first_value(
            normalized,
            ["company_name", "company", "title", "name", "business name", "place name"],
        )
        country = first_value(normalized, ["country", "market", "region", "location country"])
        emails = split_multi_value(first_value(normalized, ["email", "emails", "company email", "contact email"]))
        phones = split_multi_value(first_value(normalized, ["phone", "phones", "phone number", "telephone"]))
        conn.execute(
            """
            INSERT INTO leads (company_name, website, source, country, status, emails, phones)
            VALUES (?, ?, ?, ?, 'new', ?, ?)
            ON CONFLICT(website) DO UPDATE SET
                company_name = COALESCE(NULLIF(excluded.company_name, ''), leads.company_name),
                source = COALESCE(excluded.source, leads.source),
                country = COALESCE(NULLIF(excluded.country, ''), leads.country),
                emails = CASE
                    WHEN excluded.emails IS NOT NULL AND excluded.emails != '[]' THEN excluded.emails
                    ELSE leads.emails
                END,
                phones = CASE
                    WHEN excluded.phones IS NOT NULL AND excluded.phones != '[]' THEN excluded.phones
                    ELSE leads.phones
                END,
                updated_at = CURRENT_TIMESTAMP
            """,
            (company_name, website, source, country, json.dumps(emails), json.dumps(phones)),
        )
        imported += 1
    conn.commit()
    return imported


def import_discovered_rows(conn, rows: list[dict]) -> dict[str, int]:
    processed = 0
    created = 0
    updated = 0
    for row in rows:
        website = normalize_url(row.get("website", ""))
        if not website:
            continue
        existed = conn.execute("SELECT 1 FROM leads WHERE website = ?", (website,)).fetchone() is not None
        phones = split_multi_value(row.get("phone", ""))
        country = row.get("country", "")
        conn.execute(
            """
            INSERT INTO leads (company_name, website, source, country, status, phones)
            VALUES (?, ?, ?, ?, 'new', ?)
            ON CONFLICT(website) DO UPDATE SET
                company_name = COALESCE(NULLIF(excluded.company_name, ''), leads.company_name),
                source = COALESCE(excluded.source, leads.source),
                country = COALESCE(NULLIF(excluded.country, ''), leads.country),
                phones = CASE
                    WHEN excluded.phones IS NOT NULL AND excluded.phones != '[]' THEN excluded.phones
                    ELSE leads.phones
                END,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                row.get("company_name", ""),
                website,
                row.get("source", "discovered"),
                country,
                json.dumps(phones),
            ),
        )
        processed += 1
        if existed:
            updated += 1
        else:
            created += 1
    conn.commit()
    return {"processed": processed, "created": created, "updated": updated}


def first_value(row: dict, keys: list[str]) -> str:
    for key in keys:
        value = row.get(key)
        if value:
            return str(value).strip()
    return ""


def split_multi_value(value: str) -> list[str]:
    if not value:
        return []
    parts = []
    for chunk in str(value).replace(";", ",").replace("\n", ",").split(","):
        clean = chunk.strip().lower()
        if clean:
            parts.append(clean)
    return sorted(set(parts))


if __name__ == "__main__":
    raise SystemExit(main())
