from __future__ import annotations

import os
import smtplib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from trade_outreach.background_jobs import match_task_for_command
from trade_outreach.cli import main
from trade_outreach.db import connect, init_db


class IndependentAutoPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "outreach.sqlite3")
        self.queries_file = Path(self.temp_dir.name) / "queries.txt"
        self.countries_file = Path(self.temp_dir.name) / "countries.txt"
        self.queries_file.write_text("steel pipe distributor\n", encoding="utf-8")
        self.countries_file.write_text("Greece\n", encoding="utf-8")
        conn = connect(self.db_path)
        init_db(conn)
        conn.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def auto_args(self) -> list[str]:
        return [
            "auto-pipeline",
            "--cycles",
            "1",
            "--dry-run",
            "--discovery-interval-seconds",
            "0",
            "--processing-interval-seconds",
            "0",
            "--idle-sleep-seconds",
            "0",
            "--queries-file",
            str(self.queries_file),
            "--countries-file",
            str(self.countries_file),
        ]

    def test_all_three_queues_run_without_waiting_for_each_other(self) -> None:
        calls: list[str] = []

        def fake_step(name: str, argv: list[str]) -> int:
            calls.append(name)
            return 0

        with patch.dict(os.environ, {"OUTREACH_DB": self.db_path}, clear=False), patch(
            "trade_outreach.cli.run_cli_step", side_effect=fake_step
        ):
            self.assertEqual(main(self.auto_args()), 0)

        self.assertIn("discover companies", calls)
        self.assertIn("scrape emails", calls)
        self.assertIn("generate drafts", calls)
        self.assertIn("approve drafts", calls)
        self.assertIn("send approved drafts", calls)

    def test_one_queue_failure_does_not_prevent_other_queues(self) -> None:
        calls: list[str] = []

        def fake_step(name: str, argv: list[str]) -> int:
            calls.append(name)
            return 1 if name == "discover companies" else 0

        with patch.dict(os.environ, {"OUTREACH_DB": self.db_path}, clear=False), patch(
            "trade_outreach.cli.run_cli_step", side_effect=fake_step
        ):
            self.assertEqual(main(self.auto_args()), 1)

        self.assertIn("send approved drafts", calls)
        self.assertIn("generate drafts", calls)

    def test_approved_backlog_pauses_heavy_queues_but_keeps_sending(self) -> None:
        conn = connect(self.db_path)
        conn.executemany(
            "INSERT INTO email_drafts (lead_id, recipient, subject, body, approved) "
            "VALUES (1, ?, 'Subject', 'Body', 1)",
            [(f"info{index}@company.org",) for index in range(100)],
        )
        conn.commit()
        conn.close()
        calls: list[str] = []
        with patch.dict(os.environ, {"OUTREACH_DB": self.db_path}, clear=False), patch(
            "trade_outreach.cli.run_cli_step", side_effect=lambda name, argv: calls.append(name) or 0
        ):
            self.assertEqual(main(self.auto_args()), 0)
        self.assertEqual(calls, ["send approved drafts"])

    def test_unsupported_internationalized_recipient_does_not_block_next_draft(self) -> None:
        conn = connect(self.db_path)
        conn.executemany(
            "INSERT INTO leads (id, company_name, website) VALUES (?, 'Company', ?)",
            [(1, "https://first.company.org"), (2, "https://second.company.org")],
        )
        conn.executemany(
            "INSERT INTO email_drafts (lead_id, recipient, subject, body, approved) "
            "VALUES (?, ?, 'Subject', 'Body', 1)",
            [(1, "ınfo@company.org"), (2, "info@company.org")],
        )
        conn.commit()
        conn.close()
        slot = Mock()
        with patch.dict(
            os.environ,
            {"OUTREACH_DB": self.db_path, "MAIL_FROM": "sales@sender.org", "MAIL_CC": "", "MAIL_BCC": ""},
            clear=False,
        ), patch("trade_outreach.cli.global_sent_state", return_value=(0, None)), patch(
            "trade_outreach.cli.acquire_send_slot", return_value=(slot, "")
        ), patch(
            "trade_outreach.cli.send_email",
            side_effect=[smtplib.SMTPNotSupportedError("SMTPUTF8 unsupported"), None],
        ) as send:
            self.assertEqual(main(["send", "--limit", "1", "--delay-seconds", "0"]), 0)
        self.assertEqual(send.call_count, 2)
        conn = connect(self.db_path)
        rows = conn.execute("SELECT approved, sent_at FROM email_drafts ORDER BY id").fetchall()
        conn.close()
        self.assertEqual(rows[0]["approved"], 0)
        self.assertIsNone(rows[0]["sent_at"])
        self.assertIsNotNone(rows[1]["sent_at"])

    def test_draft_generation_releases_write_lock_between_llm_requests(self) -> None:
        conn = connect(self.db_path)
        conn.executemany(
            "INSERT INTO leads (company_name, website, status, score, emails) "
            "VALUES (?, ?, 'scored', 10, ?)",
            [
                ("First Company", "https://first.company.org", '["info@first.company.org"]'),
                ("Second Company", "https://second.company.org", '["info@second.company.org"]'),
            ],
        )
        conn.commit()
        conn.close()
        other_writer_succeeded = []

        def generate(_settings, _prompt):
            if not other_writer_succeeded:
                other_writer_succeeded.append(False)
            else:
                second = sqlite3.connect(self.db_path, timeout=0.1)
                try:
                    second.execute("UPDATE leads SET company_name = company_name WHERE id = 1")
                    second.commit()
                    other_writer_succeeded.append(True)
                finally:
                    second.close()
            return "We supply products to business buyers."

        with patch.dict(
            os.environ, {"OUTREACH_DB": self.db_path, "USE_LLM": "1"}, clear=False
        ), patch("trade_outreach.cli.generate_text", side_effect=generate):
            self.assertEqual(main(["draft", "--limit", "2", "--min-score", "0"]), 0)
        self.assertEqual(other_writer_succeeded, [False, True])

    def test_new_leads_are_scraped_before_old_failures(self) -> None:
        conn = connect(self.db_path)
        conn.execute(
            "INSERT INTO leads (company_name, website, status, score) VALUES ('Old failure', 'https://old.example', 'scrape_failed', 99)"
        )
        conn.execute(
            "INSERT INTO leads (company_name, website, status, score) VALUES ('New lead', 'https://new.example', 'new', 0)"
        )
        conn.commit()
        conn.close()
        result = {"page_text": "public page", "emails": [], "phones": []}
        with patch.dict(os.environ, {"OUTREACH_DB": self.db_path}, clear=False), patch(
            "trade_outreach.cli.scrape_site", return_value=result
        ):
            self.assertEqual(main(["scrape", "--limit", "1", "--retry-failed"]), 0)
        conn = connect(self.db_path)
        statuses = {
            row["company_name"]: row["status"]
            for row in conn.execute("SELECT company_name, status FROM leads")
        }
        conn.close()
        self.assertEqual(statuses["New lead"], "no_email")
        self.assertEqual(statuses["Old failure"], "scrape_failed")


class BackgroundJobRecoveryTests(unittest.TestCase):
    def test_matches_task_specific_auto_pipeline(self) -> None:
        tasks = [
            {
                "id": "default",
                "name": "Steel Pipe",
                "queries_file": "data/tasks/default/queries.txt",
                "countries_file": "data/tasks/default/countries.txt",
            },
            {
                "id": "valve-a36c84",
                "name": "Valve",
                "queries_file": "data/tasks/valve-a36c84/queries.txt",
                "countries_file": "data/tasks/valve-a36c84/countries.txt",
            },
        ]
        command = (
            "python3 -m trade_outreach.cli auto-pipeline "
            "--queries-file data/tasks/valve-a36c84/queries.txt "
            "--countries-file data/tasks/valve-a36c84/countries.txt"
        )
        self.assertEqual(match_task_for_command(command, tasks)["id"], "valve-a36c84")
        self.assertIsNone(match_task_for_command("caffeinate " + command, tasks))


if __name__ == "__main__":
    unittest.main()
