"""Evidence-driven merging of the same job across boards.

A posting often appears on the board that hosts it (LinkedIn, Indeed, ...) and
on an aggregator that links to it (Google Jobs), or on several boards that send
applicants to the same employer page.  Merging keeps one entity per job and
remembers every source record.

A merge needs positive evidence.  The signals are, strongest first:

``site_job_mapping``
    One record links to another record's native (site, job id) pair - an
    aggregator whose URL is the board's posting, for instance.
``native_job_id``
    The same board and the same native job id (one board's own duplicate).
``canonical_apply_url``
    The same employer application URL once tracking parameters, scheme, host
    case, default ports and trailing slashes are normalised.  Only URLs that
    address one posting count; a company careers home page never merges.
``employer_job_id``
    The same employer requisition id for the same employer (the ATS
    organisation in the URL, or the normalised company name when the URL has
    none).
``job_url``
    The same canonical job page URL.

Title and company similarity is never evidence: two records with the same
title and company but no shared identifier stay separate.

A conflicting field is taken from the highest-priority source; ``MergeConfig``
sets that order, per field too.  The merged record keeps every source record
and, for each conflict, every original value with the chosen one.

Withdrawing a source is a re-merge: :func:`retract_sources` drops the withdrawn
records and merges the rest, so the other sources' contributions survive.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobspy.model import JobPost, Location

# Boards that host postings rank above aggregators such as Google, whose
# descriptions are snippets and whose identifiers are its own.
DEFAULT_SOURCE_PRIORITY: tuple[str, ...] = (
    "linkedin",
    "indeed",
    "glassdoor",
    "zip_recruiter",
    "bayt",
    "naukri",
    "bdjobs",
    "hellowork",
    "google",
)

SITE_PREFIXES = {
    "in": "indeed",
    "li": "linkedin",
    "gd": "glassdoor",
    "zr": "zip_recruiter",
    "bayt": "bayt",
    "nk": "naukri",
    "bd": "bdjobs",
    "hw": "hellowork",
    "go": "google",
}

# The merged entity's own columns, not job attributes.
ENTITY_FIELDS = frozenset(
    {
        "source_count",
        "sources",
        "provenance",
        "merge_evidence",
    }
)

# Fields the merge loop never takes from a source record: the entity owns them.
IDENTITY_FIELDS = ENTITY_FIELDS | {"id", "site", "entity_id"}

# Values from every source are kept, in priority order, instead of picking one.
ADDITIVE_FIELDS = frozenset({"job_type", "emails", "skills"})

# Merge evidence from strongest to weakest; ``merge_evidence`` is listed in
# this order and the first is what a reviewer should check.
EVIDENCE_ORDER: tuple[str, ...] = (
    "site_job_mapping",
    "native_job_id",
    "canonical_apply_url",
    "employer_job_id",
    "job_url",
)

# Pay is one fact: taking min_amount from one board and interval from another
# would invent a salary.  Choose the group from one source.
PAY_FIELDS = ("salary_source", "interval", "min_amount", "max_amount", "currency")

_TRACKING_PARAMS = frozenset(
    {
        "gclid",
        "fbclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "ref",
        "referrer",
        "source",
        "src",
        "trk",
        "trackingid",
        "campaign",
        "lever-source",
        "lever-origin",
        "gh_src",
    }
)

# Path segments that address a search page or a company's home page, not a job.
_GENERIC_SEGMENTS = frozenset(
    {
        "job",
        "jobs",
        "career",
        "careers",
        "jobsearch",
        "search",
        "open-roles",
        "openings",
        "vacancies",
        "vacancy",
        "about",
        "home",
        "company",
        "companies",
        "employers",
        "en",
        "fr",
        "de",
    }
)

_ID_SEGMENT = re.compile(
    r"(?:"
    r"\d{3,}"  # numeric ids
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F-]{4,}"  # UUIDs
    r"|[A-Za-z0-9_-]*\d[A-Za-z0-9_-]{3,}"  # mixed ids of four or more characters
    r")"
)
_REQ_SUFFIX = re.compile(r"(?:^|[_-])([A-Za-z]{0,3}-?\d{2,})$")

_HOST_ALIASES = {"job-boards.greenhouse.io": "boards.greenhouse.io"}

_ATS_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "workable.com",
    "smartrecruiters.com",
    "myworkdayjobs.com",
    "myworkdaysite.com",
    "workday.com",
    "ashbyhq.com",
    "icims.com",
    "taleo.net",
    "bamboohr.com",
    "jobvite.com",
    "recruitee.com",
    "personio.de",
    "teamtailor.com",
)

_LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "gmbh",
        "ag",
        "sa",
        "sas",
        "bv",
        "nv",
        "plc",
        "pvt",
        "private",
        "company",
        "group",
        "holdings",
    }
)

_ATS_ID_PARAMS = (
    "gh_jid",
    "jobId",
    "job_id",
    "requisitionId",
    "requisition_id",
    "reqId",
    "req_id",
    "postingId",
    "posting_id",
    "jobReqId",
    "jobid",
)


@dataclass
class MergeConfig:
    """How records are merged.

    ``source_priority`` orders sources for conflicting fields; a site missing
    from it ranks after the listed ones.  ``field_priority`` overrides the order
    for named fields.  ``max_entities`` caps the merged entities (the unified
    count) - never the source records.  ``embed_source_records`` keeps every
    original record in the entity; turning it off keeps only their keys and
    URLs.
    """

    source_priority: Sequence[str] = field(
        default_factory=lambda: DEFAULT_SOURCE_PRIORITY
    )
    field_priority: Mapping[str, Sequence[str]] = field(default_factory=dict)
    max_entities: int | None = None
    embed_source_records: bool = True

    def __post_init__(self) -> None:
        self.source_priority = tuple(dict.fromkeys(self.source_priority))
        self.field_priority = {
            name: tuple(dict.fromkeys(priority))
            for name, priority in self.field_priority.items()
        }
        if self.max_entities is not None and self.max_entities < 0:
            raise ValueError("max_entities must not be negative")

    def rank(self, site: str | None, field_name: str | None = None) -> int:
        priority = self.field_priority.get(field_name or "", self.source_priority)
        try:
            return priority.index(site or "")
        except ValueError:
            return len(priority)

    def ordered(self, records: Sequence[JobPost]) -> list[JobPost]:
        return sorted(
            records,
            key=lambda record: (
                self.rank(record.site),
                record.site or "",
                record.id or record.job_url,
            ),
        )


def _text(value: object) -> str:
    if isinstance(value, Enum):
        value = value.value
    return str(value)


def _is_empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    if isinstance(value, float):
        return value != value  # NaN
    return False


def _same(left: object, right: object) -> bool:
    def key(value: object) -> object:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, Location):
            return (_text(value.city), _text(value.state), _text(value.country))
        if isinstance(value, (list, tuple)):
            return tuple(key(item) for item in value)
        return value

    return key(left) == key(right)


def _display(value: object) -> object:
    """A value as plain data for provenance, a location as one string."""
    if isinstance(value, Location):
        return value.display_location()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_display(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


def _source_value(value: object) -> object:
    """A value as plain data for a source record, keeping its structure so the
    record can be read back as a JobPost unchanged."""
    if isinstance(value, Location):
        return {
            name: _source_value(getattr(value, name))
            for name in Location.model_fields
            if getattr(value, name) is not None
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_source_value(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


def _source_data(record: JobPost) -> dict[str, object]:
    """One source record as plain data, dropping empty values.

    The source keeps its own ``site`` and ``id``: those identify the record,
    while the entity's ``id`` identifies the merged job.
    """
    data: dict[str, object] = {}
    for name in JobPost.model_fields:
        if name in ENTITY_FIELDS:
            continue
        value = _source_value(getattr(record, name))
        if not _is_empty(value):
            data[name] = value
    data["source_key"] = source_key(record)
    return data


def source_key(record: JobPost) -> str:
    """A stable key for one original record."""
    return f"{record.site or '?'}:{record.id or record.job_url}"


def _native_id(record: JobPost) -> str | None:
    """The board's own id for the posting.

    Its job URL is the board's own record of the id, so it wins; the ``id``
    column's board prefix is the fallback.
    """
    for site, native in parse_site_job_refs(record.job_url):
        if site == record.site:
            return native
    if record.id:
        prefix, _, rest = str(record.id).partition("-")
        if rest and SITE_PREFIXES.get(prefix) == record.site:
            return rest
    return None


def _host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    host = host.removeprefix("www.")
    return _HOST_ALIASES.get(host, host)


def canonical_url(url: str | None) -> str | None:
    """A URL with the parts that identify a posting kept and the parts that
    only track a click removed."""
    if not url or not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https", ""):
        return None
    host = _host(url)
    if not host:
        return None
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name.lower() not in _TRACKING_PARAMS and not name.lower().startswith("utm_")
    ]
    query.sort()
    return urlunsplit(("https", netloc, path, urlencode(query), ""))


def _is_specific_job_url(url: str) -> bool:
    """Whether a URL names one posting rather than a search or careers home."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    segments = [segment for segment in parts.path.split("/") if segment]
    last = segments[-1].lower() if segments else ""
    if not segments or last in _GENERIC_SEGMENTS:
        return False
    if any(_ID_SEGMENT.fullmatch(segment) for segment in segments):
        return True
    return any(
        name.lower()
        in {
            "jk",
            "jl",
            "jid",
            "jobid",
            "job_id",
            "gh_jid",
            "jobreqid",
            "requisitionid",
            "reqid",
            "postingid",
            "currentjobid",
        }
        and len(value) >= 2
        for name, value in parse_qsl(parts.query)
    )


def _specific(url: str | None) -> str | None:
    canonical = canonical_url(url)
    if not canonical:
        return None
    if _is_specific_job_url(canonical) or employer_identity(canonical):
        return canonical
    return None


def parse_site_job_refs(url: str | None) -> set[tuple[str, str]]:
    """The (site, native job id) pairs a URL points at, for known boards."""
    if not url:
        return set()
    try:
        parts = urlsplit(url)
    except ValueError:
        return set()
    host = _host(url)
    pattern = (parts.path or "") + ("?" + parts.query if parts.query else "")
    refs: set[tuple[str, str]] = set()

    def add(site: str, pattern_match: re.Match[str] | None) -> None:
        if pattern_match:
            refs.add((site, pattern_match.group(1)))

    if "linkedin." in host:
        add("linkedin", re.search(r"/jobs/view/(?:[^/?#]+?-)?(\d+)", pattern))
        add("linkedin", re.search(r"[?&]currentJobId=(\d+)", pattern))
    elif "indeed." in host:
        add("indeed", re.search(r"[?&]jk=([A-Za-z0-9]+)", pattern))
        add("indeed", re.search(r"/viewjob/([A-Za-z0-9]+)", pattern))
    elif "glassdoor." in host:
        add("glassdoor", re.search(r"[?&]jl=(\d+)", pattern))
        add(
            "glassdoor",
            re.search(r"/job-listing/[^?#]*?(\d+)", pattern),
        )
    elif "ziprecruiter." in host:
        add(
            "zip_recruiter",
            re.search(r"/(?:jobs?|c/[^/]+/job)/([A-Za-z0-9_-]+)", pattern),
        )
        add("zip_recruiter", re.search(r"[?&](?:jobId|jid)=([A-Za-z0-9_-]+)", pattern))
    elif "bayt." in host:
        add("bayt", re.search(r"/jobs/(\d+)", pattern))
    elif "naukri." in host:
        add("naukri", re.search(r"[?&]jobId=(\d+)", pattern))
        add("naukri", re.search(r"-(\d{5,})(?:[?#]|$)", pattern))
    elif "bdjobs." in host:
        add("bdjobs", re.search(r"/details/(\d+)", pattern))
    elif "hellowork." in host:
        add("hellowork", re.search(r"/(\d+)\.html", pattern))
        add("hellowork", re.search(r"[?&]id=(\d+)", pattern))
    return refs


def _normalise_company(name: str | None) -> str | None:
    if not name:
        return None
    value = name.casefold().replace("&", " and ")
    value = re.sub(r"[^\w\s]", " ", value)
    tokens = [token for token in value.split() if token]
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    cleaned = " ".join(tokens)
    return cleaned or None


def _ats_host(host: str) -> str | None:
    for ats in _ATS_HOSTS:
        if host == ats or host.endswith("." + ats):
            return ats
    return None


def _ats_org(url: str) -> str | None:
    """The employer's slug in an ATS URL."""
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    for segment in segments:
        lowered = segment.lower()
        if (
            lowered in _GENERIC_SEGMENTS
            or _ID_SEGMENT.fullmatch(segment)
            or re.fullmatch(r"[a-z]{2}(?:[-_][a-z]{2,4})?", lowered)
        ):
            # a locale such as "en-US" is not the employer
            continue
        return lowered
    return None


def _ats_posting_id(url: str, ats: str) -> str | None:
    parts = urlsplit(url)
    query = {name.lower(): value for name, value in parse_qsl(parts.query)}
    for name in _ATS_ID_PARAMS:
        if value := query.get(name.lower()):
            return value
    segments = [segment for segment in parts.path.split("/") if segment]
    if ats in {"myworkdayjobs.com", "myworkdaysite.com", "workday.com"} and segments:
        match = _REQ_SUFFIX.search(segments[-1])
        if match:
            return match.group(1)
    for segment in reversed(segments):
        if _ID_SEGMENT.fullmatch(segment):
            return segment
    return None


def employer_identity(url: str | None) -> tuple[str, str] | None:
    """(organisation, posting id) from an employer application URL."""
    if not url:
        return None
    try:
        urlsplit(url)
    except ValueError:
        return None
    host = _host(url)
    if not host:
        return None
    ats = _ats_host(host)
    if ats:
        posting = _ats_posting_id(url, ats)
        if not posting:
            return None
        return (_ats_org(url) or host, posting)
    for name, value in parse_qsl(urlsplit(url).query):
        if value and name.lower() in {item.lower() for item in _ATS_ID_PARAMS}:
            return (host, value)
    return None


def employer_job_id(url: str | None) -> str | None:
    """The employer's requisition id carried by an application URL."""
    identity = employer_identity(url)
    return identity[1] if identity else None


def prepare_record(record: JobPost) -> JobPost:
    """Fill ``employer_job_id`` from the application URL when the board gave
    the URL but not the id."""
    if record.employer_job_id or not record.job_url_direct:
        return record
    found = employer_job_id(record.job_url_direct)
    return record.model_copy(update={"employer_job_id": found}) if found else record


class _UnionFind:
    def __init__(self, count: int) -> None:
        self.parent = list(range(count))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> bool:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return False
        self.parent[right_root] = left_root
        return True


@dataclass
class _Link:
    signal: str
    key: str
    indices: tuple[int, ...]


def _evidence_links(records: Sequence[JobPost]) -> list[_Link]:
    """Every pair of records that shares positive evidence."""
    native: dict[tuple[str, str], list[int]] = {}
    job_urls: dict[str, list[int]] = {}
    apply_urls: dict[str, list[int]] = {}
    employer: dict[tuple[str, str], list[int]] = {}

    for index, record in enumerate(records):
        site = record.site or ""
        if site and (native_id := _native_id(record)):
            native.setdefault((site, native_id), []).append(index)
        if url := _specific(record.job_url):
            job_urls.setdefault(url, []).append(index)
        if url := _specific(record.job_url_direct):
            apply_urls.setdefault(url, []).append(index)
        if record.employer_job_id:
            identity = employer_identity(record.job_url_direct)
            org = identity[0] if identity else _normalise_company(record.company)
            if org:
                employer.setdefault((org, record.employer_job_id), []).append(index)

    links: list[_Link] = []
    seen: set[tuple[str, str, tuple[int, ...]]] = set()

    def add(signal: str, key: str, members: Iterable[int]) -> None:
        unique = tuple(sorted(dict.fromkeys(members)))
        if len(unique) < 2 or (signal, key, unique) in seen:
            return
        seen.add((signal, key, unique))
        links.append(_Link(signal, key, unique))

    for (site, native_id), members in native.items():
        add("native_job_id", f"{site}:{native_id}", members)
    for url, members in job_urls.items():
        add("job_url", url, members)
    for url, members in apply_urls.items():
        add("canonical_apply_url", url, members)
    for (org, posting), members in employer.items():
        add("employer_job_id", f"{org}:{posting}", members)

    # A record that links to another record's native job key maps sites.
    for index, record in enumerate(records):
        own = (record.site or "", _native_id(record) or "")
        refs = parse_site_job_refs(record.job_url) | parse_site_job_refs(
            record.job_url_direct
        )
        for ref in refs:
            if ref == own:
                continue
            add("site_job_mapping", f"{ref[0]}:{ref[1]}", [index, *native.get(ref, [])])

    # An application URL may equal another record's job page.
    for url, members in apply_urls.items():
        add("canonical_apply_url", url, [*members, *job_urls.get(url, [])])

    return links


def _merge_value(
    records: Sequence[JobPost],
    name: str,
    config: MergeConfig,
) -> tuple[object, dict[str, object] | None]:
    """A field's value, and when sources disagree, its provenance."""
    ordered = sorted(
        records,
        key=lambda record: (
            config.rank(record.site, name),
            record.site or "",
            record.id or record.job_url,
        ),
    )
    candidates = [
        (record, getattr(record, name))
        for record in ordered
        if not _is_empty(getattr(record, name))
    ]
    if not candidates:
        return None, None

    if name in ADDITIVE_FIELDS:
        merged: list[object] = []
        for _, candidate in candidates:
            for item in candidate if isinstance(candidate, list) else [candidate]:
                if not any(_same(item, kept) for kept in merged):
                    merged.append(item)
        value: object = merged
        rule = "union"
        contributed = {
            tuple(
                _text(item)
                for item in (candidate if isinstance(candidate, list) else [candidate])
            )
            for _, candidate in candidates
        }
        if len(contributed) < 2:
            return value, None
    else:
        value = candidates[0][1]
        rule = "source_priority"
        if all(_same(value, candidate) for _, candidate in candidates):
            return value, None

    chosen = candidates[0]
    return value, {
        "value": _display(value),
        "site": None if name in ADDITIVE_FIELDS else chosen[0].site,
        "source_id": None if name in ADDITIVE_FIELDS else chosen[0].id,
        "rule": rule,
        "alternatives": [
            {"value": _display(candidate), "site": record.site, "source_id": record.id}
            for record, candidate in candidates[1:]
        ],
    }


def _merge_pay(
    records: Sequence[JobPost], config: MergeConfig
) -> tuple[dict[str, object], dict[str, object] | None]:
    ranked = [
        record
        for record in config.ordered(records)
        if any(not _is_empty(getattr(record, name)) for name in PAY_FIELDS)
    ]
    if not ranked:
        return {}, None
    winner = ranked[0]

    def pay(record: JobPost) -> dict[str, object]:
        return {
            name: _display(getattr(record, name))
            for name in PAY_FIELDS
            if not _is_empty(getattr(record, name))
        }

    update = {name: getattr(winner, name) for name in PAY_FIELDS}
    conflicting = [record for record in ranked[1:] if pay(record) != pay(winner)]
    if not conflicting:
        return update, None
    return update, {
        "value": pay(winner),
        "site": winner.site,
        "source_id": winner.id,
        "rule": "source_priority",
        "alternatives": [
            {"value": pay(record), "site": record.site, "source_id": record.id}
            for record in ranked[1:]
        ],
    }


def _entity_id(records: Sequence[JobPost]) -> str:
    keys = sorted(source_key(record) for record in records)
    digest = hashlib.sha1("\n".join(keys).encode("utf-8")).hexdigest()
    return f"entity-{digest[:12]}"


def _build_entity(
    records: Sequence[JobPost],
    config: MergeConfig,
    links: Sequence[_Link],
) -> JobPost:
    entity_id = _entity_id(records)
    if len(records) == 1:
        only = records[0]
        return only.model_copy(
            update={
                "entity_id": entity_id,
                "source_count": 1,
                "sources": (
                    [_source_data(only)] if config.embed_source_records else None
                ),
            }
        )

    ranked = config.ordered(records)
    base = ranked[0]
    update: dict[str, object] = {"id": entity_id, "entity_id": entity_id}
    provenance: dict[str, dict[str, object]] = {}

    pay_update, pay_provenance = _merge_pay(records, config)
    update |= pay_update
    if pay_provenance:
        provenance["pay"] = pay_provenance

    for name in JobPost.model_fields:
        if name in IDENTITY_FIELDS or name in PAY_FIELDS:
            continue
        value, conflict = _merge_value(records, name, config)
        update[name] = value
        if conflict:
            provenance[name] = conflict

    keys = {index: source_key(record) for index, record in enumerate(records)}
    update |= {
        "site": base.site,
        "source_count": len(ranked),
        "sources": (
            [_source_data(record) for record in ranked]
            if config.embed_source_records
            else [
                {
                    name: value
                    for name, value in _source_data(record).items()
                    if name in ("source_key", "site", "id", "job_url", "job_url_direct")
                }
                for record in ranked
            ]
        ),
        "provenance": provenance or None,
        "merge_evidence": [
            {
                "signal": link.signal,
                "key": link.key,
                "sources": [keys[index] for index in link.indices],
            }
            for link in sorted(
                links,
                key=lambda item: (
                    EVIDENCE_ORDER.index(item.signal)
                    if item.signal in EVIDENCE_ORDER
                    else len(EVIDENCE_ORDER),
                    item.key,
                ),
            )
        ]
        or None,
    }
    return base.model_copy(update=update)


def merge_jobs(
    records: Iterable[JobPost],
    config: MergeConfig | None = None,
) -> list[JobPost]:
    """Merge records that share positive evidence into one entity each.

    Records without evidence are returned unchanged.  A conflicting field comes
    from the highest-priority source and keeps its other values in
    ``provenance``; every original record stays in ``sources``.

    ``config.max_entities`` caps the merged entities, never the source records:
    that is the unified count.  Entities are ordered newest first, then by
    source priority.
    """
    config = config or MergeConfig()
    originals = [prepare_record(record) for record in records]

    unique: list[JobPost] = []
    seen: set[str] = set()
    for record in originals:
        key = source_key(record)
        if key not in seen:
            seen.add(key)
            unique.append(record)
    if not unique:
        return []

    links = _evidence_links(unique)
    union = _UnionFind(len(unique))
    for link in links:
        first, *rest = link.indices
        for other in rest:
            union.union(first, other)

    groups: dict[int, list[int]] = {}
    for index in range(len(unique)):
        groups.setdefault(union.find(index), []).append(index)

    merged: list[JobPost] = []
    for members in groups.values():
        member_set = set(members)
        local = {index: position for position, index in enumerate(members)}
        group_links = []
        for link in links:
            indices = tuple(
                local[index] for index in link.indices if index in member_set
            )
            if len(indices) >= 2:
                group_links.append(_Link(link.signal, link.key, indices))
        merged.append(
            _build_entity([unique[index] for index in members], config, group_links)
        )

    merged.sort(
        key=lambda record: (
            -(record.date_posted.toordinal() if record.date_posted else 0),
            config.rank(record.site),
            (record.title or "").casefold(),
        )
    )
    if config.max_entities is not None:
        merged = merged[: config.max_entities]
    return merged


def expand_sources(record: JobPost) -> list[dict[str, object]]:
    """The original records behind an entity; the record itself when it has
    none (it was not merged)."""
    if record.sources:
        return list(record.sources)
    return [_source_data(record)]


def retract_sources(
    records: Iterable[JobPost | Mapping[str, object]],
    *,
    sites: Iterable[str] | None = None,
    ids: Iterable[str] | None = None,
    source_keys: Iterable[str] | None = None,
    config: MergeConfig | None = None,
) -> list[JobPost]:
    """Re-merge original records after withdrawing some sources.

    ``records`` are the original records, or an entity's ``sources`` dicts.
    A withdrawn source contributes nothing; every other record stays and is
    available for each field, so withdrawing the source a value came from
    promotes the next source instead of deleting the entity.  Withdrawing every
    source of an entity removes that entity.
    """
    withdrawn_sites = set(sites or ())
    withdrawn_ids = set(ids or ())
    withdrawn_keys = set(source_keys or ())
    config = config or MergeConfig()
    kept = [
        record
        for record in map(_as_record, records)
        if record.site not in withdrawn_sites
        and (record.id or "") not in withdrawn_ids
        and source_key(record) not in withdrawn_keys
    ]
    return merge_jobs(kept, config)


def _as_record(record: JobPost | Mapping[str, object]) -> JobPost:
    """A record, or one of an entity's ``sources`` dicts, as a JobPost."""
    if isinstance(record, JobPost):
        return record
    fields = {
        name: value
        for name, value in record.items()
        if name in JobPost.model_fields and name != "source_key"
    }
    fields.setdefault("title", "")
    fields.setdefault("job_url", "")
    return JobPost.model_validate(fields)


__all__ = [
    "DEFAULT_SOURCE_PRIORITY",
    "EVIDENCE_ORDER",
    "MergeConfig",
    "canonical_url",
    "employer_identity",
    "employer_job_id",
    "expand_sources",
    "merge_jobs",
    "parse_site_job_refs",
    "prepare_record",
    "retract_sources",
    "source_key",
]
