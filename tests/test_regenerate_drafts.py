from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from trade_outreach.cli import main
from trade_outreach.dashboard import JOBS, run_command, start_background_command
from trade_outreach.db import connect, init_db


class RegenerateDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "outreach.sqlite3")
        conn = connect(self.db_path)
        init_db(conn)
        for name in ("Pending Co", "Approved Co", "Sent Co"):
            conn.execute(
                "INSERT INTO leads (company_name, website, page_text) VALUES (?, ?, ?)",
                (name, f"https://{name.split()[0].lower()}.example", "Public distributor website"),
            )
            lead_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO email_drafts (lead_id, recipient, subject, body, approved, sent_at) "
                "VALUES (?, ?, 'Old subject', 'Old body', ?, ?)",
                (lead_id, f"info@{name.split()[0].lower()}.example", int(name == "Approved Co"),
                 "2026-09-01 00:00:00" if name == "Sent Co" else None),
            )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def rows(self):
        conn = connect(self.db_path)
        rows = {row["company_name"]: dict(row) for row in conn.execute(
            "SELECT l.company_name, d.subject, d.body, d.approved, d.sent_at "
            "FROM email_drafts d JOIN leads l ON l.id = d.lead_id"
        )}
        conn.close()
        return rows

    def test_bulk_regenerates_approved_first_and_leaves_sent_untouched(self) -> None:
        prompts = []

        def generate(_settings, prompt):
            prompts.append(prompt)
            return "Fresh personalized opening."

        output = io.StringIO()
        with patch.dict(os.environ, {"OUTREACH_DB": self.db_path, "USE_LLM": "1",
                                          "COMPANY_NAME": "Our Company"}), patch(
            "trade_outreach.cli.generate_text", side_effect=generate
        ), redirect_stdout(output):
            self.assertEqual(main(["regenerate-unsent-drafts", "--no-cache"]), 0)

        rows = self.rows()
        self.assertIn("Approved Co", prompts[0])
        for name in ("Pending Co", "Approved Co"):
            self.assertIn("Fresh personalized opening.", rows[name]["body"])
            self.assertIn("Our Company", rows[name]["body"])
            self.assertEqual(rows[name]["approved"], 0)
        self.assertEqual(rows["Sent Co"]["body"], "Old body")
        self.assertEqual(rows["Sent Co"]["sent_at"], "2026-09-01 00:00:00")
        self.assertIn("previously approved: 1", output.getvalue())

    def test_llm_failure_preserves_approved_draft_and_continues(self) -> None:
        def generate(_settings, prompt):
            if "Approved Co" in prompt:
                raise RuntimeError("temporary LLM error")
            return "Fresh personalized opening."

        with patch.dict(os.environ, {"OUTREACH_DB": self.db_path, "USE_LLM": "1"}), patch(
            "trade_outreach.cli.generate_text", side_effect=generate
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["regenerate-unsent-drafts", "--no-cache"]), 1)

        rows = self.rows()
        self.assertEqual(rows["Approved Co"]["body"], "Old body")
        self.assertEqual(rows["Approved Co"]["approved"], 1)
        self.assertIn("Fresh personalized opening.", rows["Pending Co"]["body"])

    def test_dashboard_starts_bulk_regeneration_as_background_job(self) -> None:
        task = {"id": "test", "name": "Test", "db_path": self.db_path,
                "queries_file": "queries.txt", "countries_file": "countries.txt"}
        with patch("trade_outreach.dashboard.start_background_command", return_value={"job_id": "123"}) as start:
            self.assertEqual(run_command("regenerate_unsent", task), {"job_id": "123"})
        self.assertEqual(start.call_args.kwargs["kind"], "regenerate_unsent")

    def test_regeneration_waits_for_active_auto_pipeline(self) -> None:
        task = {"id": "test", "name": "Test"}
        running_job = {"id": "auto-1", "kind": "auto_pipeline", "task_id": "test", "running": True}
        with patch("trade_outreach.dashboard.refresh_jobs"), patch.dict(JOBS, {"auto-1": running_job}, clear=True):
            result = start_background_command(["python3"], task=task, kind="regenerate_unsent")
        self.assertFalse(result["ok"])
        self.assertEqual(result["job_id"], "auto-1")


if __name__ == "__main__":
    unittest.main()
