from __future__ import annotations

import json
import os

import pandas as pd

from jobspy.model import JobPost

FIELDS = list(JobPost.model_fields)
PREVIEW = (
    "site title company location date_posted job_type is_remote min_amount max_amount"
    " currency interval job_url"
).split()
MERGE_FIELDS = ["entity_id", "source_count", "sources", "provenance", "merge_evidence"]
DEFAULT_SITES = ["indeed", "linkedin", "zip_recruiter", "glassdoor"]
REGIONAL = "bayt (Middle East), naukri (India), bdjobs (Bangladesh), hellowork (France)"
FORMATS = (".csv", ".json", ".jsonl")


def _missing(value: object) -> bool:
    if isinstance(value, (list, tuple, dict)):
        return not value
    return value is None or (isinstance(value, float) and value != value)


def records(jobs: pd.DataFrame, names: list[str]) -> list[dict[str, object]]:
    return [
        {name: rec[name] for name in names if not _missing(rec[name])}
        for rec in jobs[names].to_dict("records")
    ]


def expand_frame(jobs: pd.DataFrame) -> pd.DataFrame:
    """One row per source record behind each merged entity.

    Rows of jobs that were not merged are left as they are.  Reachable from the
    CLI with ``--expand`` and from MCP with ``expand=True``.
    """
    rows: list[dict[str, object]] = []
    for record in jobs.to_dict("records"):
        row = {str(name): value for name, value in record.items()}
        sources = row.get("sources")
        if not isinstance(sources, (list, tuple)) or not sources:
            rows.append(row)
            continue
        # A source row is that board's own record, linked to its entity; the
        # entity's merged values are not copied over it.
        for source in sources:
            if isinstance(source, dict):
                expanded: dict[str, object] = {
                    name: None
                    for name in jobs.columns
                    if name not in ("sources", "provenance", "merge_evidence")
                }
                expanded.update(source)
                expanded["entity_id"] = row.get("entity_id")
                rows.append(expanded)
    if not rows:
        return jobs
    frame = pd.DataFrame(rows)
    for name in jobs.columns:
        if name not in frame:
            frame[name] = None
    return frame[list(jobs.columns)]


def dumps(value: object) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))


def lines(jobs: pd.DataFrame, names: list[str]) -> list[str]:
    return [dumps(rec) + "\n" for rec in records(jobs, names)]


def save_path(path: str) -> str:
    if not path.lower().endswith(FORMATS):
        raise ValueError(f"{path}: use .csv, .json or .jsonl")
    return path


def save(jobs: pd.DataFrame, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if path.lower().endswith(".csv"):
        jobs.to_csv(path, index=False)
    else:
        with open(path, "w", encoding="utf-8") as saved:
            if path.lower().endswith(".json"):
                saved.write(dumps(records(jobs, FIELDS)))
            else:
                saved.writelines(lines(jobs, FIELDS))


def commas(text: str) -> list[str]:
    return [name for part in text.split(",") if (name := part.strip())]
