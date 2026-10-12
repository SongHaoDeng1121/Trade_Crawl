from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from trade_outreach.cli import main
from trade_outreach.config import get_settings
from trade_outreach.db import connect, init_db
from trade_outreach.discovery_schedule import (
    ScheduledCountry,
    country_discovery_status,
    plan_country_schedule,
    record_cooldown_skips,
    record_country_round,
    scheduled_queries,
)


class DiscoveryScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "outreach.sqlite3")
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self.settings = replace(
            get_settings(),
            db_path=self.db_path,
            discovery_no_new_cooldown_rounds=2,
            discovery_cooldown_hours=24,
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def test_legacy_countries_wait_for_zero_coverage_countries(self) -> None:
        self.conn.execute(
            "INSERT INTO leads (company_name, website, country) VALUES ('Greek lead', 'https://greek.example', 'Greece')"
        )
        self.conn.commit()
        plan = plan_country_schedule(self.conn, self.settings, ["Greece", "Monaco"], 2, 3)
        self.assertEqual([item.country for item in plan.countries], ["Monaco"])
        self.assertEqual(plan.countries[0].round_no, 1)
        self.assertEqual(plan.countries[0].phase, "baseline")

    def test_cooldown_skips_once_and_other_country_advances(self) -> None:
        for country in ["Greece", "Germany"]:
            self.conn.execute(
                "INSERT INTO leads (company_name, website, country) VALUES (?, ?, ?)",
                (country, f"https://{country.lower()}.example", country),
            )
        for round_no in [1, 2]:
            self.conn.execute(
                """
                INSERT INTO discovery_country_rounds (
                    country, round_no, target_count, lead_count_before, query_count, new_website_count, status
                ) VALUES ('Greece', ?, 40, 1, 1, 0, 'completed')
                """,
                (round_no,),
            )
            self.conn.execute(
                """
                INSERT INTO discovery_country_rounds (
                    country, round_no, target_count, lead_count_before, query_count, new_website_count, status
                ) VALUES ('Germany', ?, 60, 1, 1, 1, 'completed')
                """,
                (round_no,),
            )
        self.conn.commit()
        plan = plan_country_schedule(self.conn, self.settings, ["Greece", "Germany"], 2, 3)
        self.assertEqual([item.country for item in plan.countries], ["Germany"])
        self.assertEqual([item[0] for item in plan.cooldown_skips], ["Greece"])
        record_cooldown_skips(self.conn, plan.cooldown_skips)
        self.conn.commit()
        second = plan_country_schedule(self.conn, self.settings, ["Greece", "Germany"], 2, 3)
        self.assertFalse(second.cooldown_skips)

    def test_all_countries_continue_and_finish_round_before_next_round(self) -> None:
        for country in ["Greece", "Germany"]:
            self.conn.execute(
                "INSERT INTO leads (company_name, website, country) VALUES (?, ?, ?)",
                (country, f"https://continue-{country.lower()}.example", country),
            )
        self.conn.commit()
        first = plan_country_schedule(self.conn, self.settings, ["Greece", "Germany"], 1, 3)
        self.assertEqual(first.countries[0].round_no, 2)
        first_country = first.countries[0].country
        record_country_round(
            self.conn,
            first.countries[0],
            query_count=1,
            result_count=1,
            new_count=1,
            failed_count=0,
        )
        self.conn.commit()
        second = plan_country_schedule(self.conn, self.settings, ["Greece", "Germany"], 1, 3)
        self.assertNotEqual(second.countries[0].country, first_country)
        self.assertEqual(second.countries[0].round_no, 2)

    def test_expansion_uses_new_keywords_local_language_and_city(self) -> None:
        specs = [
            ("steel pipe stockholder", ""),
            ("steel tube stockholder", ""),
            ("steel pipe distributor", ""),
            ("industrial pipe supplier", ""),
            ("προμηθευτής χαλύβδινων σωλήνων", "Greece"),
        ]
        scheduled = ScheduledCountry("Greece", 2, "expansion", 40, 7, 2)
        rows = scheduled_queries(self.conn, specs, scheduled)
        self.assertEqual(len([row for row in rows if not row["is_local"]]), 2)
        self.assertTrue(any(row["is_local"] for row in rows))
        self.assertTrue(all("Athens" in row["query"] for row in rows))
        self.assertTrue(any(row["base_query"] == "steel pipe distributor" for row in rows))

    def test_preview_does_not_write_and_execute_records_history(self) -> None:
        queries_file = Path(self.temp_dir.name) / "queries.txt"
        countries_file = Path(self.temp_dir.name) / "countries.txt"
        queries_file.write_text("steel pipe distributor\nGreece :: προμηθευτής σωλήνων\n", encoding="utf-8")
        countries_file.write_text("Greece\n", encoding="utf-8")
        rows = [{"company_name": "Example", "website": "https://example.gr", "source": "test"}]
        env = {"OUTREACH_DB": self.db_path}
        argv = [
            "discover-batch",
            "--queries-file",
            str(queries_file),
            "--countries-file",
            str(countries_file),
            "--scheduled-countries",
            "1",
            "--limit-queries",
            "1",
            "--count",
            "5",
        ]
        with patch.dict(os.environ, env, clear=False), patch("trade_outreach.cli.discover_websites", return_value=rows):
            self.assertEqual(main(argv), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM discovery_runs").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 0)
        with patch.dict(os.environ, env, clear=False), patch("trade_outreach.cli.discover_websites", return_value=rows):
            self.assertEqual(main(argv + ["--execute"]), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM discovery_runs").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM discovery_country_rounds").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 1)
        status = country_discovery_status(self.conn, self.settings, ["Greece"])["Greece"]
        self.assertEqual(status["rounds_completed"], 1)


if __name__ == "__main__":
    unittest.main()
