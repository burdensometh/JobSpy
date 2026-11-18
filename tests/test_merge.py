from __future__ import annotations

import json
import unittest

from jobspy.merge import (
    DEFAULT_SOURCE_PRIORITY,
    MergeConfig,
    canonical_url,
    employer_identity,
    expand_sources,
    merge_jobs,
    parse_site_job_refs,
    prepare_record,
    retract_sources,
    source_key,
)
from jobspy.model import JobPost, JobType, Location


def post(**overrides: object) -> JobPost:
    fields: dict[str, object] = {
        "title": "Engineer",
        "job_url": "https://example.com/job/1",
    }
    fields.update(overrides)
    return JobPost(**fields)


class EvidenceTests(unittest.TestCase):
    def test_site_job_mapping_merges_aggregator_into_board(self) -> None:
        board = post(
            id="li-101",
            site="linkedin",
            job_url="https://www.linkedin.com/jobs/view/101",
        )
        aggregator = post(
            id="go-1",
            site="google",
            job_url="https://www.linkedin.com/jobs/view/101?utm_source=google",
        )
        entities = merge_jobs([board, aggregator])
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].source_count, 2)
        signals = {item["signal"] for item in entities[0].merge_evidence or []}
        self.assertIn("site_job_mapping", signals)

    def test_native_job_id_merges_one_board_duplicates(self) -> None:
        first = post(
            id="in-abc", site="indeed", job_url="https://www.indeed.com/viewjob?jk=abc"
        )
        second = post(
            id="in-abc-later",
            site="indeed",
            job_url="https://www.indeed.com/viewjob?jk=abc&from=feed",
        )
        entities = merge_jobs([first, second])
        self.assertEqual(len(entities), 1)
        signals = {item["signal"] for item in entities[0].merge_evidence or []}
        self.assertTrue(signals & {"native_job_id", "site_job_mapping"})

    def test_canonical_apply_url_merges_across_boards(self) -> None:
        first = post(
            id="li-1",
            site="linkedin",
            job_url="https://www.linkedin.com/jobs/view/1",
            job_url_direct="https://boards.greenhouse.io/acme/jobs/5521?utm_source=linkedin",
        )
        second = post(
            id="in-2",
            site="indeed",
            job_url="https://www.indeed.com/viewjob?jk=2",
            job_url_direct="https://boards.greenhouse.io/acme/jobs/5521?gh_src=indeed",
        )
        entities = merge_jobs([first, second])
        self.assertEqual(len(entities), 1)
        self.assertIn(
            "canonical_apply_url",
            {item["signal"] for item in entities[0].merge_evidence or []},
        )

    def test_employer_job_id_merges_a_different_ats_link(self) -> None:
        first = post(
            id="li-1",
            site="linkedin",
            job_url="https://www.linkedin.com/jobs/view/1",
            job_url_direct="https://boards.greenhouse.io/acme/jobs/5521",
            company="Acme",
        )
        second = post(
            id="in-2",
            site="indeed",
            job_url="https://www.indeed.com/viewjob?jk=2",
            employer_job_id="5521",
            company="Acme Inc",
        )
        entities = merge_jobs([first, second])
        self.assertEqual(len(entities), 1)
        self.assertIn(
            "employer_job_id",
            {item["signal"] for item in entities[0].merge_evidence or []},
        )

    def test_title_and_company_similarity_is_not_evidence(self) -> None:
        first = post(
            id="li-1",
            site="linkedin",
            title="Nurse",
            company="Acme",
            job_url="https://www.linkedin.com/jobs/view/1",
        )
        second = post(
            id="li-2",
            site="linkedin",
            title="Nurse",
            company="Acme",
            job_url="https://www.linkedin.com/jobs/view/2",
        )
        self.assertEqual(len(merge_jobs([first, second])), 2)

    def test_corporate_careers_home_is_not_evidence(self) -> None:
        first = post(
            id="li-1",
            site="linkedin",
            job_url="https://www.linkedin.com/jobs/view/1",
            job_url_direct="https://acme.com/careers",
        )
        second = post(
            id="in-2",
            site="indeed",
            job_url="https://www.indeed.com/viewjob?jk=2",
            job_url_direct="https://acme.com/careers",
        )
        self.assertEqual(len(merge_jobs([first, second])), 2)


class ConflictTests(unittest.TestCase):
    def records(self) -> list[JobPost]:
        return [
            post(
                id="li-1",
                site="linkedin",
                title="Senior Engineer",
                company="Acme, Inc.",
                location=Location(city="Austin", state="TX", country="usa"),
                job_url="https://www.linkedin.com/jobs/view/1",
                job_url_direct="https://boards.greenhouse.io/acme/jobs/5521",
                min_amount=150000,
                max_amount=180000,
                currency="USD",
                interval="yearly",
                job_type=[JobType.FULL_TIME],
            ),
            post(
                id="in-2",
                site="indeed",
                title="Senior Engineer (Remote)",
                company="Acme",
                location=Location(city="Austin", state="TX", country="usa"),
                job_url="https://www.indeed.com/viewjob?jk=2",
                job_url_direct="https://boards.greenhouse.io/acme/jobs/5521?gh_src=x",
                min_amount=70,
                max_amount=90,
                currency="USD",
                interval="hourly",
                job_type=[JobType.CONTRACT],
            ),
        ]

    def test_priority_picks_the_value_and_keeps_alternatives(self) -> None:
        entity = merge_jobs(self.records())[0]
        self.assertEqual(entity.title, "Senior Engineer")
        title = entity.provenance["title"]
        self.assertEqual(title["site"], "linkedin")
        self.assertEqual(title["alternatives"][0]["value"], "Senior Engineer (Remote)")

    def test_pay_is_chosen_as_one_group(self) -> None:
        entity = merge_jobs(self.records())[0]
        self.assertEqual(entity.interval.value, "yearly")
        self.assertEqual(entity.min_amount, 150000)
        self.assertEqual(entity.currency, "USD")
        self.assertEqual(entity.provenance["pay"]["site"], "linkedin")

    def test_additive_fields_keep_every_value(self) -> None:
        entity = merge_jobs(self.records())[0]
        self.assertEqual(entity.job_type, [JobType.FULL_TIME, JobType.CONTRACT])

    def test_configured_priority_changes_the_choice(self) -> None:
        entity = merge_jobs(
            self.records(), MergeConfig(source_priority=["indeed", "linkedin"])
        )[0]
        self.assertEqual(entity.title, "Senior Engineer (Remote)")
        self.assertEqual(entity.min_amount, 70)

    def test_per_field_priority(self) -> None:
        entity = merge_jobs(
            self.records(),
            MergeConfig(field_priority={"title": ["indeed", "linkedin"]}),
        )[0]
        self.assertEqual(entity.title, "Senior Engineer (Remote)")
        self.assertEqual(entity.min_amount, 150000)

    def test_every_source_record_is_kept(self) -> None:
        entity = merge_jobs(self.records())[0]
        self.assertEqual(
            [source["source_key"] for source in entity.sources or []],
            ["linkedin:li-1", "indeed:in-2"],
        )
        self.assertEqual(
            (entity.sources or [{}])[1]["job_url_direct"],
            "https://boards.greenhouse.io/acme/jobs/5521?gh_src=x",
        )


class RetractionTests(unittest.TestCase):
    def records(self) -> list[JobPost]:
        return [
            post(
                id="li-1",
                site="linkedin",
                title="Senior Engineer",
                job_url="https://www.linkedin.com/jobs/view/1",
                job_url_direct="https://boards.greenhouse.io/acme/jobs/5521",
                min_amount=150000,
                interval="yearly",
                currency="USD",
            ),
            post(
                id="in-2",
                site="indeed",
                title="Senior Engineer II",
                job_url="https://www.indeed.com/viewjob?jk=2",
                job_url_direct="https://boards.greenhouse.io/acme/jobs/5521",
                min_amount=70,
                interval="hourly",
                currency="USD",
            ),
        ]

    def test_withdrawing_the_winning_source_promotes_the_next(self) -> None:
        merged = retract_sources(self.records(), sites=["linkedin"])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].title, "Senior Engineer II")
        self.assertEqual(merged[0].min_amount, 70)
        self.assertEqual(
            [source["source_key"] for source in merged[0].sources or []],
            ["indeed:in-2"],
        )

    def test_withdrawing_a_losing_source_keeps_the_winner(self) -> None:
        merged = retract_sources(
            self.records(), source_keys=[source_key(self.records()[1])]
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].title, "Senior Engineer")
        self.assertEqual(merged[0].source_count, 1)

    def test_withdrawing_every_source_removes_the_entity(self) -> None:
        records = self.records()
        self.assertEqual(
            retract_sources(
                records, source_keys=[source_key(item) for item in records]
            ),
            [],
        )

    def test_retraction_from_an_entity_sources(self) -> None:
        entity = merge_jobs(self.records())[0]
        merged = retract_sources(entity.sources or [], sites=["linkedin"])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].title, "Senior Engineer II")
        self.assertEqual(merged[0].min_amount, 70)

    def test_retraction_keeps_the_other_sources_fields(self) -> None:
        records = self.records()
        merged = retract_sources(records, sites=["indeed"])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].title, "Senior Engineer")
        self.assertEqual(merged[0].min_amount, 150000)


class CapTests(unittest.TestCase):
    def test_cap_counts_entities_not_sources(self) -> None:
        first = post(
            id="li-1",
            site="linkedin",
            job_url="https://www.linkedin.com/jobs/view/1",
            job_url_direct="https://boards.greenhouse.io/acme/jobs/5521",
        )
        second = post(
            id="in-2",
            site="indeed",
            job_url="https://www.indeed.com/viewjob?jk=2",
            job_url_direct="https://boards.greenhouse.io/acme/jobs/5521",
        )
        other = post(
            id="in-3", site="indeed", job_url="https://www.indeed.com/viewjob?jk=3"
        )
        merged = merge_jobs([first, second, other], MergeConfig(max_entities=1))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].source_count, 2)


class UrlTests(unittest.TestCase):
    def test_canonical_url_drops_tracking(self) -> None:
        self.assertEqual(
            canonical_url("http://WWW.Example.com/jobs/1/?utm_source=x&b=2&a=1#top"),
            "https://example.com/jobs/1?a=1&b=2",
        )

    def test_canonical_url_keeps_a_posting_query(self) -> None:
        self.assertEqual(
            canonical_url("https://www.indeed.com/viewjob?jk=abc"),
            "https://indeed.com/viewjob?jk=abc",
        )

    def test_site_refs_from_board_urls(self) -> None:
        self.assertEqual(
            parse_site_job_refs("https://www.linkedin.com/jobs/view/3693012711"),
            {("linkedin", "3693012711")},
        )
        self.assertEqual(
            parse_site_job_refs("https://www.indeed.com/viewjob?jk=5e409e57"),
            {("indeed", "5e409e57")},
        )

    def test_employer_identity_from_greenhouse(self) -> None:
        self.assertEqual(
            employer_identity(
                "https://boards.greenhouse.io/acme/jobs/5521?gh_jid=5521"
            ),
            ("acme", "5521"),
        )

    def test_employer_identity_from_workday_skips_the_locale(self) -> None:
        self.assertEqual(
            employer_identity(
                "https://acme.wd5.myworkdayjobs.com/en-US/acme/job/Austin/Engineer_R-12345"
            ),
            ("acme", "R-12345"),
        )

    def test_employer_identity_from_lever_and_smartrecruiters(self) -> None:
        self.assertEqual(
            employer_identity("https://jobs.lever.co/acme/abc-123-def"),
            ("acme", "abc-123-def"),
        )
        self.assertEqual(
            employer_identity("https://jobs.smartrecruiters.com/Acme/744000012345678"),
            ("acme", "744000012345678"),
        )

    def test_ats_urls_merge_across_boards(self) -> None:
        first = post(
            id="li-1",
            site="linkedin",
            job_url="https://www.linkedin.com/jobs/view/1",
            job_url_direct="https://acme.wd5.myworkdayjobs.com/en-US/acme/job/Austin/Engineer_R-12345",
        )
        second = post(
            id="in-2",
            site="indeed",
            job_url="https://www.indeed.com/viewjob?jk=2",
            job_url_direct="https://acme.wd5.myworkdayjobs.com/en-US/acme/job/Austin/Engineer_R-12345?source=indeed",
        )
        self.assertEqual(len(merge_jobs([first, second])), 1)

    def test_different_employers_with_one_requisition_number_stay_apart(self) -> None:
        first = post(
            id="li-1",
            site="linkedin",
            company="Acme",
            job_url="https://www.linkedin.com/jobs/view/1",
            employer_job_id="123",
        )
        second = post(
            id="li-2",
            site="linkedin",
            company="Globex",
            job_url="https://www.linkedin.com/jobs/view/2",
            employer_job_id="123",
        )
        self.assertEqual(len(merge_jobs([first, second])), 2)

    def test_prepare_record_fills_the_requisition_id(self) -> None:
        record = post(
            id="go-1",
            site="google",
            job_url_direct="https://acme.com/jobs/1?jobId=REQ-42",
        )
        prepared = prepare_record(record)
        self.assertEqual(prepared.employer_job_id, "REQ-42")


class ExpandTests(unittest.TestCase):
    def test_expanding_returns_the_original_records(self) -> None:
        first = post(
            id="li-1",
            site="linkedin",
            job_url="https://www.linkedin.com/jobs/view/1",
            job_url_direct="https://boards.greenhouse.io/acme/jobs/5521",
        )
        second = post(
            id="in-2",
            site="indeed",
            job_url="https://www.indeed.com/viewjob?jk=2",
            job_url_direct="https://boards.greenhouse.io/acme/jobs/5521",
        )
        entity = merge_jobs([first, second])[0]
        sources = expand_sources(entity)
        self.assertEqual(len(sources), 2)
        self.assertEqual(json.dumps(sources[0], default=str)[:1], "{")


class DefaultsTests(unittest.TestCase):
    def test_default_priority_puts_hosting_boards_first(self) -> None:
        self.assertLess(
            DEFAULT_SOURCE_PRIORITY.index("linkedin"),
            DEFAULT_SOURCE_PRIORITY.index("google"),
        )


if __name__ == "__main__":
    unittest.main()
