from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

import jobspy
import jobspy.cli as cli
from jobspy.merge import retract_sources, source_key
from jobspy.model import JobPost, Location, Scraper, Site

LINKEDIN = JobPost(
    id="li-101",
    title="Senior Engineer",
    company="Acme, Inc.",
    job_url="https://www.linkedin.com/jobs/view/101",
    job_url_direct="https://boards.greenhouse.io/acme/jobs/5521?utm_source=linkedin",
    location=Location(city="Austin", state="TX", country="usa"),
    min_amount=150000,
    max_amount=180000,
    currency="USD",
    interval="yearly",
    description="LinkedIn description",
)
GOOGLE = JobPost(
    id="go-zz",
    title="Senior Engineer (Remote)",
    company="Acme Inc",
    job_url="https://www.linkedin.com/jobs/view/101?utm_source=google",
    description="Google snippet",
)
INDEED = JobPost(
    id="in-abc",
    title="Senior Engineer",
    company="Acme",
    job_url="https://www.indeed.com/viewjob?jk=abc",
    job_url_direct="https://boards.greenhouse.io/acme/jobs/5521?gh_src=indeed",
    interval="hourly",
    min_amount=70,
    max_amount=90,
    currency="USD",
    description="Indeed description",
)


class FakeScraper(Scraper):
    records: list[JobPost] = []

    def scrape(self, scraper_input: object) -> list[JobPost]:
        return list(self.records)


class LinkedIn(FakeScraper):
    records = [LINKEDIN]


class Google(FakeScraper):
    records = [GOOGLE]


class Indeed(FakeScraper):
    records = [INDEED]


class BoardSetup(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = dict(jobspy.SCRAPER_MAPPING)
        jobspy.SCRAPER_MAPPING.update(
            {
                Site.LINKEDIN: LinkedIn,
                Site.GOOGLE: Google,
                Site.INDEED: Indeed,
            }
        )

    def tearDown(self) -> None:
        jobspy.SCRAPER_MAPPING.clear()
        jobspy.SCRAPER_MAPPING.update(self.saved)


class ScrapeJobsTests(BoardSetup):
    def test_off_by_default(self) -> None:
        jobs = jobspy.scrape_jobs(site_name=["linkedin", "indeed", "google"])
        self.assertEqual(len(jobs), 3)
        self.assertTrue(jobs["entity_id"].isna().all())
        self.assertTrue(jobs["sources"].isna().all())

    def test_merge_produces_one_entity_with_every_source(self) -> None:
        jobs = jobspy.scrape_jobs(
            site_name=["linkedin", "indeed", "google"], merge_duplicates=True
        )
        self.assertEqual(len(jobs), 1)
        row = jobs.iloc[0]
        self.assertEqual(row["source_count"], 3)
        self.assertEqual(
            [source["source_key"] for source in row["sources"]],
            ["linkedin:li-101", "indeed:in-abc", "google:go-zz"],
        )
        self.assertEqual(row["site"], "linkedin")
        self.assertEqual(row["min_amount"], 150000)
        self.assertEqual(row["title"], "Senior Engineer")
        self.assertIn("title", row["provenance"])
        signals = {item["signal"] for item in row["merge_evidence"]}
        self.assertIn("site_job_mapping", signals)
        self.assertIn("canonical_apply_url", signals)

    def test_unmatched_records_stay_separate(self) -> None:
        class Extra(FakeScraper):
            records = [
                JobPost(
                    id="in-other",
                    title="Senior Engineer",
                    company="Acme",
                    job_url="https://www.indeed.com/viewjob?jk=other",
                )
            ]

        jobspy.SCRAPER_MAPPING[Site.INDEED] = Extra
        jobs = jobspy.scrape_jobs(
            site_name=["linkedin", "indeed"], merge_duplicates=True
        )
        self.assertEqual(len(jobs), 2)

    def test_max_results_caps_entities_and_keeps_sources(self) -> None:
        class Extra(FakeScraper):
            records = [
                JobPost(
                    id="in-other",
                    title="Nurse",
                    company="Other",
                    job_url="https://www.indeed.com/viewjob?jk=other",
                )
            ]

        jobspy.SCRAPER_MAPPING[Site.INDEED] = Extra
        jobs = jobspy.scrape_jobs(
            site_name=["linkedin", "indeed"],
            merge_duplicates=True,
            max_results=1,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs.iloc[0]["source_count"], 1)

    def test_configured_priority_changes_the_winner(self) -> None:
        jobs = jobspy.scrape_jobs(
            site_name=["linkedin", "indeed"],
            merge_duplicates=True,
            source_priority=["indeed", "linkedin"],
        )
        self.assertEqual(jobs.iloc[0]["min_amount"], 70)

    def test_merge_only_options_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "merge_duplicates"):
            jobspy.scrape_jobs(site_name=["linkedin"], source_priority=["linkedin"])
        with self.assertRaisesRegex(ValueError, "merge_duplicates"):
            jobspy.scrape_jobs(site_name=["linkedin"], max_results=2)

    def test_retracting_a_source_keeps_the_rest(self) -> None:
        jobs = jobspy.scrape_jobs(
            site_name=["linkedin", "indeed", "google"], merge_duplicates=True
        )
        remaining = retract_sources(jobs.iloc[0]["sources"], sites=["linkedin"])
        # Google only linked to the LinkedIn posting, so it is no longer evidence
        # for Indeed; both remaining records survive with their own fields.
        self.assertEqual(
            sorted(source_key(record) for record in remaining),
            ["google:go-zz", "indeed:in-abc"],
        )
        indeed = next(item for item in remaining if item.site == "indeed")
        self.assertEqual(indeed.title, "Senior Engineer")
        self.assertEqual(indeed.min_amount, 70)
        google = next(item for item in remaining if item.site == "google")
        self.assertEqual(google.description, "Google snippet")

    def test_expand_frame_returns_one_row_per_source(self) -> None:
        from jobspy.records import expand_frame

        jobs = jobspy.scrape_jobs(
            site_name=["linkedin", "indeed", "google"], merge_duplicates=True
        )
        expanded = expand_frame(jobs)
        self.assertEqual(len(expanded), 3)
        self.assertEqual(list(expanded["site"]), ["linkedin", "indeed", "google"])
        self.assertEqual(expanded["entity_id"].nunique(), 1)
        self.assertEqual(list(expanded["min_amount"].dropna()), [150000, 70])


class CliTests(BoardSetup):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def test_merge_flag_prints_one_entity(self) -> None:
        code, out = self.run_cli(
            [
                "engineer",
                "--site",
                "linkedin,indeed,google",
                "--merge",
                "--fields",
                "id,site,source_count",
            ]
        )
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_count"], 3)

    def test_expand_flag_prints_each_board(self) -> None:
        code, out = self.run_cli(
            [
                "engineer",
                "--site",
                "linkedin,indeed,google",
                "--merge",
                "--expand",
                "--fields",
                "site,id,min_amount",
            ]
        )
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(
            [(row["site"], row["id"]) for row in rows],
            [
                ("linkedin", "li-101"),
                ("indeed", "in-abc"),
                ("google", "go-zz"),
            ],
        )
        self.assertEqual([row.get("min_amount") for row in rows], [150000, 70, None])

    def test_expand_without_merge_is_a_usage_error(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.run_cli(["engineer", "--site", "linkedin", "--expand"])
        self.assertEqual(raised.exception.code, 2)

    def test_output_file_keeps_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "jobs.json")
            code, _ = self.run_cli(
                [
                    "engineer",
                    "--site",
                    "linkedin,indeed",
                    "--merge",
                    "--output",
                    path,
                ]
            )
            self.assertEqual(code, 0)
            saved = json.load(open(path, encoding="utf-8"))
            self.assertEqual(len(saved), 1)
            self.assertEqual(
                [source["source_key"] for source in saved[0]["sources"]],
                ["linkedin:li-101", "indeed:in-abc"],
            )


class McpTests(BoardSetup):
    def test_search_returns_entities_and_details_keep_sources(self) -> None:
        from jobspy import mcp

        result = mcp.search_jobs(
            search_term="engineer",
            site_name=["linkedin", "indeed", "google"],
            merge_duplicates=True,
        )
        payload = result.structured_content
        self.assertEqual(payload["entity_count"], 1)
        self.assertEqual(payload["counts"], {"linkedin": 1, "indeed": 1, "google": 1})
        entity_id = payload["jobs"][0]["id"]
        details = mcp.get_job_details(
            search_id=payload["search_id"], job_ids=[entity_id]
        ).structured_content
        entity = details["jobs"][0]
        self.assertEqual(len(entity["sources"]), 3)
        self.assertIn("pay", entity["provenance"])
        self.assertTrue(entity["merge_evidence"])

    def test_expand_returns_source_records(self) -> None:
        from jobspy import mcp

        payload = mcp.search_jobs(
            search_term="engineer",
            site_name=["linkedin", "indeed", "google"],
            merge_duplicates=True,
            expand=True,
        ).structured_content
        self.assertEqual(
            [(job["site"], job["id"]) for job in payload["jobs"]],
            [
                ("linkedin", "li-101"),
                ("indeed", "in-abc"),
                ("google", "go-zz"),
            ],
        )

    def test_expand_without_merge_is_an_error(self) -> None:
        from mcp.server.mcpserver.exceptions import ToolError

        from jobspy import mcp

        with self.assertRaises(ToolError):
            mcp.search_jobs(site_name=["linkedin"], expand=True)


class RetractionTests(unittest.TestCase):
    def test_retract_by_source_key(self) -> None:
        records = [LINKEDIN, INDEED]
        merged = retract_sources(records, source_keys=[source_key(LINKEDIN)])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].title, "Senior Engineer")
        self.assertEqual(merged[0].source_count, 1)


if __name__ == "__main__":
    unittest.main()
