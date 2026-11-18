from __future__ import annotations

import queue
import threading
import warnings
from collections.abc import Generator, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import ValidationError

from jobspy.bayt import Bayt
from jobspy.bdjobs import BDJobs
from jobspy.glassdoor import Glassdoor
from jobspy.google import Google
from jobspy.hellowork import HelloWork
from jobspy.indeed import Indeed
from jobspy.linkedin import LinkedIn
from jobspy.model import (
    Country,
    DescriptionFormat,
    JobBatch,
    JobPost,
    JobType,
    Scraper,
    ScraperInput,
    Site,
    SiteResult,
    SiteStatus,
    StreamEvent,
)
from jobspy.naukri import Naukri
from jobspy.records import frame
from jobspy.util import (
    configure_logging,
)
from jobspy.ziprecruiter import ZipRecruiter

__all__ = [
    "scrape_jobs",
    "stream_jobs",
    "JobBatch",
    "SiteResult",
    "SiteStatus",
    "StreamEvent",
]

SCRAPER_MAPPING: dict[Site, type[Scraper]] = {
    Site.LINKEDIN: LinkedIn,
    Site.INDEED: Indeed,
    Site.ZIP_RECRUITER: ZipRecruiter,
    Site.GLASSDOOR: Glassdoor,
    Site.GOOGLE: Google,
    Site.BAYT: Bayt,
    Site.NAUKRI: Naukri,
    Site.BDJOBS: BDJobs,
    Site.HELLOWORK: HelloWork,
}


def _scraper_input(arguments: dict[str, Any]) -> ScraperInput:
    try:
        scraper_input = ScraperInput.model_validate(
            {
                name: value
                for name, value in arguments.items()
                if not (value is None or isinstance(value, str) and not value)
            }
        )
    except ValidationError as e:
        messages = []
        for error in e.errors():
            name, value = error["loc"][0], error["input"]
            shown = type(value).__name__ if name == "proxies" else repr(value)
            messages.append(f"Invalid {name}: {shown}. {error['msg']}")
        raise ValueError("; ".join(messages)) from None
    if scraper_input.linkedin_fetch_description:
        warnings.warn(
            "linkedin_fetch_description is deprecated; use fetch_description",
            DeprecationWarning,
            stacklevel=3,
        )
    return scraper_input


def stream_jobs(
    site_name: str | Site | Iterable[str | Site] | None = None,
    search_term: str | None = None,
    google_search_term: str | None = None,
    location: str | None = None,
    distance: int | None = 50,
    is_remote: bool | None = False,
    job_type: str | JobType | None = None,
    easy_apply: bool | None = None,
    results_wanted: int | None = 15,
    country_indeed: str | Country | None = "usa",
    proxies: str | Iterable[str] | None = None,
    ca_cert: str | Path | None = None,
    description_format: str | DescriptionFormat | None = "markdown",
    linkedin_fetch_description: bool | None = False,
    linkedin_company_ids: Iterable[int] | None = None,
    offset: int | None = 0,
    hours_old: int | None = None,
    enforce_annual_salary: bool | None = False,
    verbose: int | None = 1,
    user_agent: str | None = None,
    fetch_description: bool | None = False,
    buffer_pages: int = 4,
) -> Generator[StreamEvent, None, None]:
    """
    Streams jobs from each job board as pages are parsed, instead of waiting
    for every board to finish the way scrape_jobs does. Takes the same
    arguments as scrape_jobs.

    Yields JobBatch events (one parsed page per event, jobs stamped with the
    site and annualised when enforce_annual_salary is set) interleaved across
    boards as they arrive, then exactly one SiteResult per board with its
    terminal status: SiteStatus.EXHAUSTED (no more jobs), LIMIT_REACHED
    (results_wanted delivered) or FAILED (the jobs it gave are partial; see
    its error).

    At most buffer_pages batches are buffered; a slow consumer blocks the
    boards' producers instead of growing memory. Closing the generator (or
    breaking out of the loop and dropping it) cancels the remaining pages and
    closes the boards' sessions.

    :raises ValueError: an argument is invalid
    """
    arguments = dict(locals())
    arguments.pop("buffer_pages")
    scraper_input = _scraper_input(arguments)
    configure_logging(None if verbose is None else scraper_input.verbose)
    return _event_stream(scraper_input, max(1, buffer_pages))


def _event_stream(
    scraper_input: ScraperInput, buffer_pages: int
) -> Generator[StreamEvent, None, None]:
    sites = list(dict.fromkeys(scraper_input.site_name))
    cancel = threading.Event()
    events: queue.Queue[StreamEvent] = queue.Queue(maxsize=buffer_pages)

    def offer(event: StreamEvent) -> bool:
        """Blocks while the buffer is full (backpressure); False when cancelled."""
        while not cancel.is_set():
            try:
                events.put(event, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def produce(site: Site) -> None:
        scraper = SCRAPER_MAPPING[site]()
        try:
            pages = scraper.iter_pages(scraper_input, cancel)
            for page in pages:
                if cancel.is_set():
                    pages.close()
                    return
                jobs = [
                    (
                        job.annualised() if scraper_input.enforce_annual_salary else job
                    ).model_copy(update={"site": site.value})
                    for job in page
                ]
                if jobs and not offer(JobBatch(site, jobs)):
                    pages.close()
                    return
        except Exception as error:  # scrapers handle their own; this is a bug
            scraper.fail(f"{type(error).__name__}: {error}")
        scraper.log.info("finished scraping")
        if cancel.is_set():
            return
        status = scraper.status
        if status is None:
            status = SiteStatus.FAILED if scraper.error else SiteStatus.EXHAUSTED
        offer(SiteResult(site, status, scraper.error, list(scraper.warnings)))

    with ThreadPoolExecutor(max_workers=len(sites)) as executor:
        try:
            for site in sites:
                executor.submit(produce, site)
            remaining = len(sites)
            while remaining:
                event = events.get()
                if isinstance(event, SiteResult):
                    remaining -= 1
                yield event
        finally:
            cancel.set()


def scrape_jobs(
    site_name: str | Site | Iterable[str | Site] | None = None,
    search_term: str | None = None,
    google_search_term: str | None = None,
    location: str | None = None,
    distance: int | None = 50,
    is_remote: bool | None = False,
    job_type: str | JobType | None = None,
    easy_apply: bool | None = None,
    results_wanted: int | None = 15,
    country_indeed: str | Country | None = "usa",
    proxies: str | Iterable[str] | None = None,
    ca_cert: str | Path | None = None,
    description_format: str | DescriptionFormat | None = "markdown",
    linkedin_fetch_description: bool | None = False,
    linkedin_company_ids: Iterable[int] | None = None,
    offset: int | None = 0,
    hours_old: int | None = None,
    enforce_annual_salary: bool | None = False,
    verbose: int | None = 1,
    user_agent: str | None = None,
    fetch_description: bool | None = False,
) -> pd.DataFrame:
    """
    Scrapes job data from job boards concurrently
    :param linkedin_fetch_description: Deprecated since 1.2.0: use fetch_description;
        will be removed in 2.0.
    :return: Pandas DataFrame containing job data
    :raises ValueError: an argument is invalid
    """
    rows: list[JobPost] = []
    errors: dict[str, str] = {}
    site_warnings: dict[str, list[str]] = {}
    for event in stream_jobs(
        site_name=site_name,
        search_term=search_term,
        google_search_term=google_search_term,
        location=location,
        distance=distance,
        is_remote=is_remote,
        job_type=job_type,
        easy_apply=easy_apply,
        results_wanted=results_wanted,
        country_indeed=country_indeed,
        proxies=proxies,
        ca_cert=ca_cert,
        description_format=description_format,
        linkedin_fetch_description=linkedin_fetch_description,
        linkedin_company_ids=linkedin_company_ids,
        offset=offset,
        hours_old=hours_old,
        enforce_annual_salary=enforce_annual_salary,
        verbose=verbose,
        user_agent=user_agent,
        fetch_description=fetch_description,
    ):
        if isinstance(event, JobBatch):
            rows.extend(event.jobs)
        else:
            if event.error:
                errors[event.site.value] = event.error
            if event.warnings:
                site_warnings[event.site.value] = event.warnings

    jobs_df = frame(rows)
    jobs_df.attrs = {"errors": errors, "warnings": site_warnings}
    return jobs_df
