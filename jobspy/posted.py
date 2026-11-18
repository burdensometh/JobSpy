from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo


class Precision(Enum):
    """How much of a posting time the board actually stated."""

    INSTANT = "instant"  # an exact moment, kept to the second
    DAY = "day"  # a calendar date and nothing more
    RELATIVE = "relative"  # "2 days ago": an interval against the reference time


class Freshness(Enum):
    """How a posting time interval sits against an hours_old cutoff."""

    FRESH = "fresh"  # every possible moment is within the window
    EXPIRED = "expired"  # every possible moment is too old
    UNCERTAIN = "uncertain"  # the interval straddles the cutoff


class UncertainPolicy(Enum):
    """What an hours_old filter does with Freshness.UNCERTAIN jobs."""

    INCLUDE = "include"  # count them towards results_wanted: they may be fresh
    EXCLUDE = "exclude"  # drop them: only the definitely fresh count


@dataclass(frozen=True)
class PostedTime:
    """When a job was posted, as the board stated it.

    raw is the board's own text ("2 days ago", "2026-11-18", an epoch stamp);
    timezone the zone the board's date or time is in (None when the board does
    not say and UTC is assumed); earliest and latest are the UTC bounds of the
    possible posting moment (None for an unbounded side). Nothing here is more
    precise than what the board gave: a bare date spans its whole day, and a
    relative age spans the unit the board rounded to.
    """

    precision: Precision
    earliest: datetime | None = None
    latest: datetime | None = None
    raw: str | None = None
    timezone: str | None = None
    day: date | None = None  # the stated date, when precision is DAY

    @classmethod
    def at(
        cls, moment: datetime, raw: str | None = None, zone: str | None = "UTC"
    ) -> PostedTime:
        """An exact moment; a naive one is read as UTC."""
        moment = _as_utc(moment)
        return cls(Precision.INSTANT, moment, moment, raw, zone)

    @classmethod
    def on(
        cls, day: date, raw: str | None = None, zone: str | None = None
    ) -> PostedTime:
        """A calendar date with no time: the whole day in its zone."""
        tz = ZoneInfo(zone) if zone else timezone.utc
        start = datetime.combine(day, time.min, tz).astimezone(timezone.utc)
        end = datetime.combine(day, time.max, tz).astimezone(timezone.utc)
        return cls(Precision.DAY, start, end, raw, zone, day)

    @classmethod
    def ago(
        cls,
        amount: int,
        unit: str,
        reference: datetime,
        raw: str | None = None,
        zone: str | None = None,
    ) -> PostedTime:
        """ "N units ago": an age of [N, N + 1) units against the reference."""
        reference = _as_utc(reference)
        if unit in ("month", "year"):
            step = 12 if unit == "year" else 1
            latest = _shift_months(reference, -amount * step)
            earliest = _shift_months(latest, -step)
        else:
            width = timedelta(seconds=_UNIT_SECONDS[unit])
            latest = reference - amount * width
            earliest = latest - width
        return cls(Precision.RELATIVE, earliest, latest, raw, zone)

    @classmethod
    def parse_relative(
        cls, text: object, reference: datetime, zone: str | None = None
    ) -> PostedTime | None:
        """English relative ages: "2 days ago", "30+ days ago", "just posted"."""
        if not isinstance(text, str):
            return None
        reference = _as_utc(reference)
        norm = " ".join(text.lower().split())
        if norm in ("just posted", "today"):
            start = datetime.combine(reference.date(), time.min, timezone.utc)
            return cls(Precision.RELATIVE, start, reference, text, zone)
        if norm == "yesterday":
            day = reference.date() - timedelta(days=1)
            start = datetime.combine(day, time.min, timezone.utc)
            end = datetime.combine(day, time.max, timezone.utc)
            return cls(Precision.RELATIVE, start, end, text, zone)
        match = re.fullmatch(
            r"(?:(\d+)|an?)(\+)? (second|minute|hour|day|week|month|year)s? ago",
            norm,
        )
        if not match:
            return None
        amount = int(match[1]) if match[1] else 1
        unit = match[3]
        if match[2]:  # "30+ days ago": at least that old, no upper bound
            latest = cls.ago(amount, unit, reference).latest
            return cls(Precision.RELATIVE, None, latest, text, zone)
        return cls.ago(amount, unit, reference, text, zone)

    @property
    def date_posted(self) -> date | None:
        """The one date a row may show, or None when no single date is honest."""
        if self.precision is Precision.INSTANT and self.latest:
            return self.latest.date()
        return self.day

    def freshness(self, hours_old: int | None, reference: datetime) -> Freshness:
        """Where the interval falls against the hours_old cutoff at reference."""
        if not hours_old:
            return Freshness.FRESH
        cutoff = _as_utc(reference) - timedelta(hours=hours_old)
        if self.latest is not None and self.latest < cutoff:
            return Freshness.EXPIRED
        if self.earliest is not None and self.earliest >= cutoff:
            return Freshness.FRESH
        return Freshness.UNCERTAIN

    def fields(self) -> dict[str, Any]:
        """The JobPost fields carrying this time and how precise it is."""
        return {
            "date_posted": self.date_posted,
            "posted_precision": self.precision,
            "posted_raw": self.raw,
            "posted_timezone": self.timezone,
            "posted_earliest": self.earliest,
            "posted_latest": self.latest,
        }


_UNIT_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
}


def fields_of(posted: PostedTime | None) -> dict[str, Any]:
    """posted.fields(), or nothing when the board gave no posting time."""
    return posted.fields() if posted else {}


def freshness_of(
    posted: PostedTime | None, hours_old: int | None, reference: datetime
) -> Freshness:
    """A posting time's freshness; an unknown time passes as fresh."""
    if posted is None:
        return Freshness.FRESH
    return posted.freshness(hours_old, reference)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _shift_months(moment: datetime, months: int) -> datetime:
    """The same day-of-month some months away, clamped to the month's length."""
    month = moment.month - 1 + months
    year = moment.year + month // 12
    month = month % 12 + 1
    day = min(moment.day, monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)
