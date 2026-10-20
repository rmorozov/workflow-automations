"""Turn a team roster and a working calendar into net capacity hours per AreaPath.

The roster is ``team-roster``'s output: one row per person with ``email``,
``classified_title`` and ``area_path``. The config workbook holds the calendar
(country weeks and exception days), absences, leave budgets, iterations, role
groups, rituals and the bug-fix and focus rates. Every person's hours are worked
out per day, taxed like a payslip (rituals, expected illness and leave, bug fixing,
interrupts) and summed into months and iterations. The output's
``dev_per_area_capacity`` sheet is the file ``ado-triage --capacity-file`` reads.
"""

from __future__ import annotations

import argparse
import calendar as calendar_module
import datetime as dt
import logging
import math
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl.utils import get_column_letter

from workflow_automations.ado_common.excel import excel_safe, keep_text_literal, publish_all

logger = logging.getLogger("team_capacity")

# Roster columns
EMAIL = "email"
CLASSIFIED_TITLE = "classified_title"
AREA_PATH = "area_path"
APPROVED = "approved"
COUNTRY = "country"
ALLOCATION = "allocation"
START_DATE = "start_date"
END_DATE = "end_date"
HOURS_PER_DAY = "hours_per_day"
VACATION_DAYS = "vacation_days"
SICK_DAYS = "sick_days"
RAMP_UP_SHARE = "ramp_up_share"
RAMP_UP_MONTHS = "ramp_up_months"

# The sheet and columns ado-triage --capacity-file reads.
CAPACITY_SHEET = "dev_per_area_capacity"
CAPACITY_AREA_COL = "AreaPath"

VACATION, SICK, OTHER = "vacation", "sick", "other"
ABSENCE_TYPES = (VACATION, SICK, OTHER)
PER_DAY, PER_ITERATION = "day", "iteration"
CALENDAR_DAYS, WORKING_DAYS = "calendar", "working"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
APPROVED_TEXT = {"TRUE", "True", "true", "1"}

# Model defaults; every one can be set in the config's settings sheet.
DEFAULT_SETTINGS: dict[str, Any] = {
    "iteration_start": None,
    "iteration_weeks": 2,
    "absences_from": None,
    "default_country": None,
    "workdays": "Mon-Fri",
    "hours_per_day": 8.0,
    "vacation_days": 30.0,
    "vacation_unit": CALENDAR_DAYS,
    "sick_days": 7.0,
    "bug_fix_quota": 0.5,
    "focus_loss": 0.12,
    "ramp_up_share": 0.5,
    "ramp_up_months": 3,
}

# Hour components in output order; net is what is left after the others.
COMPONENTS = ("gross", "absence", "ritual", "illness", "vacation", "bug_fix", "focus", "net")


class CapacityError(ValueError):
    """Invalid input; the message names every offending row."""


# ---------------------------------------------------------------------------
# Reference data: UAE private sector 2026-2027
# ---------------------------------------------------------------------------

# (date, name, status). Eid and other Hijri dates depend on the moon sighting and
# are announced shortly before; 2027 dates are projections. Check them against
# the MOHRE announcements before relying on a forecast.
UAE_HOLIDAYS: tuple[tuple[str, str, str], ...] = (
    ("2026-01-01", "New Year's Day", "announced"),
    ("2026-03-19", "Eid al-Fitr", "announced"),
    ("2026-03-20", "Eid al-Fitr", "announced"),
    ("2026-03-21", "Eid al-Fitr", "announced"),
    ("2026-05-26", "Arafat Day", "announced"),
    ("2026-05-27", "Eid al-Adha", "announced"),
    ("2026-05-28", "Eid al-Adha", "announced"),
    ("2026-05-29", "Eid al-Adha", "announced"),
    ("2026-06-15", "Hijri New Year", "announced"),
    ("2026-08-28", "Prophet's Birthday (moved to Friday)", "announced"),
    ("2026-12-02", "National Day", "expected; may be moved"),
    ("2026-12-03", "National Day", "expected; may be moved"),
    ("2027-01-01", "New Year's Day", "projected"),
    ("2027-03-09", "Eid al-Fitr", "projected; moon-dependent"),
    ("2027-03-10", "Eid al-Fitr", "projected; moon-dependent"),
    ("2027-03-11", "Eid al-Fitr", "projected; moon-dependent"),
    ("2027-05-15", "Arafat Day", "projected; moon-dependent"),
    ("2027-05-16", "Eid al-Adha", "projected; moon-dependent"),
    ("2027-05-17", "Eid al-Adha", "projected; moon-dependent"),
    ("2027-05-18", "Eid al-Adha", "projected; moon-dependent"),
    ("2027-06-06", "Hijri New Year", "projected; moon-dependent"),
    ("2027-08-14", "Prophet's Birthday", "projected; moon-dependent"),
    ("2027-12-02", "National Day", "projected"),
    ("2027-12-03", "National Day", "projected"),
)
# Ramadan working days are two hours shorter in the private sector (MOHRE, every year).
UAE_RAMADAN: tuple[tuple[str, str, str], ...] = (
    ("2026-02-19", "2026-03-18", "announced"),
    ("2027-02-08", "2027-03-08", "projected; moon-dependent"),
)
UAE_RAMADAN_HOURS = 6.0


def uae_calendar_rows() -> list[dict[str, Any]]:
    """The UAE exception days for 2026 and 2027, as ``calendar`` sheet rows."""
    holidays = {dt.date.fromisoformat(day) for day, _, _ in UAE_HOLIDAYS}
    rows = [
        {
            "country": "UAE",
            "date": dt.date.fromisoformat(day),
            "hours": 0.0,
            "name": name,
            "status": status,
        }
        for day, name, status in UAE_HOLIDAYS
    ]
    for first, last, status in UAE_RAMADAN:
        for day in _days(dt.date.fromisoformat(first), dt.date.fromisoformat(last)):
            if day.weekday() < 5 and day not in holidays:
                rows.append(
                    {
                        "country": "UAE",
                        "date": day,
                        "hours": UAE_RAMADAN_HOURS,
                        "name": "Ramadan (2 hours shorter)",
                        "status": status,
                    }
                )
    return sorted(rows, key=lambda row: row["date"])


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Country:
    name: str
    workdays: frozenset[int]
    hours: float
    vacation_days: float
    vacation_unit: str
    sick_days: float

    @property
    def working_ratio(self) -> float:
        """Working days per calendar day of a normal week (5/7 for Monday to Friday)."""
        return len(self.workdays) / 7


@dataclass(frozen=True)
class Group:
    name: str
    bug_fix: bool
    triage_pool: bool


@dataclass(frozen=True)
class Ritual:
    name: str
    per: str
    hours: float
    groups: frozenset[str] | None  # None: every group


@dataclass(frozen=True)
class Iteration:
    name: str
    start: dt.date
    end: dt.date
    phase: str = ""

    def __contains__(self, day: dt.date) -> bool:
        return self.start <= day <= self.end


@dataclass(frozen=True)
class Rate:
    row: int
    area: tuple[str, ...]  # lower-cased AreaPath parts
    start: dt.date | None
    end: dt.date | None
    phase: str
    bug_fix_quota: float | None
    focus_loss: float | None


@dataclass(frozen=True)
class Absence:
    row: int
    email: str
    start: dt.date
    end: dt.date
    kind: str
    hours: float | None  # per day; None removes whole days


@dataclass
class Person:
    row: int
    email: str
    area_path: str
    title: str
    group: Group
    country: Country
    allocation: float
    start: dt.date | None
    end: dt.date | None
    hours_per_day: float | None
    vacation_days: float
    sick_days: float
    ramp_share: float
    ramp_months: int

    def employed(self, day: dt.date) -> bool:
        return (self.start is None or day >= self.start) and (self.end is None or day <= self.end)

    def ramp(self, day: dt.date) -> float:
        if self.start is None or self.ramp_months <= 0:
            return 1.0
        return self.ramp_share if day < add_months(self.start, self.ramp_months) else 1.0


@dataclass
class Config:
    settings: dict[str, Any]
    countries: dict[str, Country]
    area_countries: dict[tuple[str, ...], str]
    calendar: dict[tuple[str, dt.date], float]
    absences: dict[str, list[Absence]]
    iterations: list[Iteration] | None
    groups: dict[str, Group]
    title_groups: dict[str, tuple[Group, float]]
    rituals: list[Ritual]
    rates: list[Rate]

    def iteration_of(self, day: dt.date) -> Iteration | None:
        if self.iterations is not None:
            return next((it for it in self.iterations if day in it), None)
        start = self.settings["iteration_start"]
        if start is None:
            return None
        length = 7 * self.settings["iteration_weeks"]
        first = start + dt.timedelta(days=((day - start).days // length) * length)
        # Named by its Monday; it ends on the last Friday but owns the weekend
        # after it, so a working Saturday still falls in an iteration.
        return Iteration(first.isoformat(), first, first + dt.timedelta(days=length - 1))

    def iteration_end(self, iteration: Iteration) -> dt.date:
        """The reported end: the last Friday of a cadence iteration, else the sheet's end."""
        if self.iterations is not None:
            return iteration.end
        return iteration.end - dt.timedelta(days=2)

    def rate(self, area_path: str, day: dt.date, key: str) -> float:
        parts = area_parts(area_path)
        for rate in self.rates:  # deepest first, then the latest start
            if not _is_prefix(rate.area, parts):
                continue
            if (rate.start and day < rate.start) or (rate.end and day > rate.end):
                continue
            value = getattr(rate, key)
            if value is not None:
                return value
        return self.settings[key]


@dataclass
class Problem:
    email: str
    area_path: str
    problem: str
    detail: str = ""


@dataclass
class Result:
    days: pd.DataFrame
    people: list[Person]
    problems: list[Problem] = field(default_factory=list)


def add_months(day: dt.date, months: int) -> dt.date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    return dt.date(year, month, min(day.day, calendar_module.monthrange(year, month)[1]))


def area_parts(area_path: str) -> tuple[str, ...]:
    return tuple(part.strip().lower() for part in str(area_path).split("\\") if part.strip())


def _is_prefix(prefix: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    return len(prefix) <= len(parts) and parts[: len(prefix)] == prefix


def _days(first: dt.date, last: dt.date) -> Iterable[dt.date]:
    for offset in range((last - first).days + 1):
        yield first + dt.timedelta(days=offset)


def scheduled_hours(person: Person, config: Config, day: dt.date) -> float:
    """Contracted hours on ``day``: the country's exception day or default week, scaled
    to the person's own day length (a 4-hour part-timer works half a short day)."""
    country = person.country
    hours = config.calendar.get((country.name, day))
    if hours is None:
        hours = country.hours if day.weekday() in country.workdays else 0.0
    if person.hours_per_day is not None and country.hours > 0:
        hours *= person.hours_per_day / country.hours
    return hours


def absent_hours(person: Person, config: Config, day: dt.date, hours: float) -> dict[str, float]:
    """Booked absence hours on ``day`` per type, together capped at the day's hours."""
    taken: dict[str, float] = {}
    left = hours
    for absence in config.absences.get(person.email, ()):
        if left <= 0:
            break
        if absence.start <= day <= absence.end:
            amount = min(left, hours if absence.hours is None else absence.hours)
            taken[absence.kind] = taken.get(absence.kind, 0.0) + amount
            left -= amount
    return taken


def _booked_calendar_days(person: Person, config: Config, first: dt.date, last: dt.date) -> float:
    """Calendar days of vacation booked in ``first``..``last``; a part day counts its share."""
    covered: dict[dt.date, float] = {}
    for absence in config.absences.get(person.email, ()):
        if absence.kind != VACATION:
            continue
        for day in _days(max(first, absence.start), min(last, absence.end)):
            share = 1.0
            if absence.hours is not None and person.country.hours > 0:
                scheduled = scheduled_hours(person, config, day)
                share = min(1.0, absence.hours / scheduled) if scheduled else 0.0
            covered[day] = min(1.0, covered.get(day, 0.0) + share)
    return sum(covered.values())


def budget_shares(
    person: Person, config: Config, year: int, window_start: dt.date, problems: list[Problem]
) -> tuple[float, float]:
    """Expected (sick, vacation) shares of the person's unbooked working time in ``year``.

    Each budget is the year's days minus the days of that type already booked,
    spread over the working time not booked yet from the window start to year end.
    The budget is prorated to the days employed and to the part of the year whose
    absences are recorded (``absences_from``, else the window start): leave before
    that date counts as taken.
    """
    first, last = dt.date(year, 1, 1), dt.date(year, 12, 31)
    year_days = (last - first).days + 1
    recorded = config.settings["absences_from"] or window_start
    if recorded > first:
        first = recorded
    if first > last:
        return 0.0, 0.0
    employed_days = sum(1 for day in _days(first, last) if person.employed(day))
    year_share = employed_days / year_days
    full_year = dt.date(year, 1, 1)
    full_share = sum(1 for day in _days(full_year, last) if person.employed(day)) / year_days
    booked = {SICK: 0.0, VACATION: 0.0}
    unbooked = 0.0
    for day in _days(first, last):
        if not person.employed(day):
            continue
        hours = scheduled_hours(person, config, day)
        if hours <= 0:
            continue
        taken = absent_hours(person, config, day, hours)
        for kind in (SICK, VACATION):
            booked[kind] += taken.get(kind, 0.0) / hours
        if day >= window_start:
            unbooked += (hours - sum(taken.values())) / hours

    country = person.country
    sick_left = person.sick_days * year_share - booked[SICK]
    # Over-booking is judged against the whole year's budget: a prorated budget is
    # only an estimate of what is left, and December leave may exceed it.
    over = {SICK: booked[SICK] - person.sick_days * full_share}
    if country.vacation_unit == CALENDAR_DAYS:
        booked_days = _booked_calendar_days(person, config, first, last)
        vacation_left = (person.vacation_days * year_share - booked_days) * country.working_ratio
        over[VACATION] = booked_days - person.vacation_days * full_share
        unit = "calendar days"
    else:
        vacation_left = person.vacation_days * year_share - booked[VACATION]
        over[VACATION] = booked[VACATION] - person.vacation_days * full_share
        unit = "working days"

    shares = []
    for kind, left in ((SICK, sick_left), (VACATION, vacation_left)):
        if over[kind] > 1e-9:
            problems.append(
                Problem(
                    person.email,
                    person.area_path,
                    f"{kind} over-booked",
                    f"{year}: {over[kind]:.1f} "
                    f"{unit if kind == VACATION else 'working days'} more than the budget",
                )
            )
        if left <= 1e-9 or unbooked <= 0:
            shares.append(0.0)
        else:
            if left > unbooked + 1e-9:
                problems.append(
                    Problem(
                        person.email,
                        person.area_path,
                        f"{kind} budget exceeds days left",
                        f"{year}: {left:.1f} days left, {unbooked:.1f} unbooked working days",
                    )
                )
            shares.append(min(1.0, max(0.0, left) / unbooked))
    return shares[0], shares[1]


def net_hours(
    available: float,
    daily_ritual: float,
    iteration_ritual: float,
    available_days: int,
    sick_share: float,
    vacation_share: float,
    bug_fix_quota: float,
    focus_loss: float,
    scale: float = 1.0,
) -> dict[str, float]:
    """One available day's hours split into components; ``scale`` is allocation x ramp-up.

    H = (h - r - R / A) x (1 - s - v) x (1 - b - focus) x scale

    Sick and vacation shares add up: both are expected days off out of the same
    unbooked days, and nobody is off twice on one day.
    """
    per_day = daily_ritual + (iteration_ritual / available_days if available_days else 0.0)
    ritual = min(available, per_day)
    after_ritual = available - ritual
    illness = after_ritual * sick_share
    vacation = after_ritual * min(vacation_share, 1.0 - sick_share)
    base = after_ritual - illness - vacation
    bug_fix = base * bug_fix_quota
    focus = base * focus_loss
    parts = {
        "ritual": ritual,
        "illness": illness,
        "vacation": vacation,
        "bug_fix": bug_fix,
        "focus": focus,
        "net": base - bug_fix - focus,
    }
    return {key: value * scale for key, value in parts.items()}


def compute_days(
    people: Sequence[Person],
    config: Config,
    first: dt.date,
    last: dt.date,
    problems: list[Problem],
) -> pd.DataFrame:
    """One row per person and day in ``first``..``last`` with every hour component."""
    # Rituals per iteration need each person's available days in the whole
    # iteration, so iterations at the edges are read in full.
    span_first, span_last = first, last
    for edge in (first, last):
        iteration = config.iteration_of(edge)
        if iteration is not None:
            span_first = min(span_first, iteration.start)
            span_last = max(span_last, iteration.end)

    rows: list[dict[str, Any]] = []
    for person in people:
        rituals_daily = sum(
            r.hours
            for r in config.rituals
            if r.per == PER_DAY and (r.groups is None or person.group.name in r.groups)
        )
        rituals_iteration = sum(
            r.hours
            for r in config.rituals
            if r.per == PER_ITERATION and (r.groups is None or person.group.name in r.groups)
        )
        shares: dict[int, tuple[float, float]] = {}
        days: list[tuple[dt.date, float, dict[str, float], Iteration | None]] = []
        available_days: dict[str, int] = defaultdict(int)
        for day in _days(span_first, span_last):
            iteration = config.iteration_of(day)
            hours = scheduled_hours(person, config, day) if person.employed(day) else 0.0
            taken = absent_hours(person, config, day, hours) if hours > 0 else {}
            if iteration is not None and hours - sum(taken.values()) > 0:
                available_days[iteration.name] += 1
            if first <= day <= last:
                days.append((day, hours, taken, iteration))
        for day, hours, taken, iteration in days:
            if day.year not in shares:
                shares[day.year] = budget_shares(person, config, day.year, first, problems)
            sick_share, vacation_share = shares[day.year]
            employed = person.employed(day)
            scale = person.allocation * person.ramp(day) if employed else 0.0
            available = hours - sum(taken.values())
            row: dict[str, Any] = {
                "email": person.email,
                "area_path": person.area_path,
                "classified_title": person.title,
                "group": person.group.name,
                "country": person.country.name,
                "date": day,
                "month": f"{day.year:04d}-{day.month:02d}",
                "iteration": iteration.name if iteration else "",
                "scale": scale,
                "working_day": 1 if employed and scheduled_hours(person, config, day) > 0 else 0,
                "gross": hours * scale,
                "absence": (hours - available) * scale,
            }
            if available > 0:
                bug_fix = config.rate(person.area_path, day, "bug_fix_quota")
                focus = config.rate(person.area_path, day, "focus_loss")
                if person.group.bug_fix and bug_fix + focus > 1 + 1e-9:
                    raise CapacityError(
                        f"rates for {person.area_path} on {day}: bug_fix_quota {bug_fix:g} + "
                        f"focus_loss {focus:g} is more than 1 (a blank cell inherits)"
                    )
                parts = net_hours(
                    available,
                    rituals_daily,
                    rituals_iteration if iteration else 0.0,
                    available_days[iteration.name] if iteration else 0,
                    sick_share,
                    vacation_share,
                    bug_fix if person.group.bug_fix else 0.0,
                    focus,
                    scale,
                )
            else:
                parts = dict.fromkeys(COMPONENTS[2:], 0.0)
            row.update(parts)
            rows.append(row)
    columns = [
        "email",
        "area_path",
        "classified_title",
        "group",
        "country",
        "date",
        "month",
        "iteration",
        "scale",
        "working_day",
        *COMPONENTS,
    ]
    return pd.DataFrame(rows, columns=columns)


# ---------------------------------------------------------------------------
# Reading and validation
# ---------------------------------------------------------------------------


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _text(value: Any) -> str:
    return "" if _blank(value) else str(value).strip()


class Errors:
    """Collects every input error so a run reports them all at once."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def add(self, message: str) -> None:
        self.messages.append(message)

    def raise_if_any(self) -> None:
        if self.messages:
            raise CapacityError("Invalid input:\n  " + "\n  ".join(self.messages))

    def date(self, value: Any, where: str) -> dt.date | None:
        if _blank(value):
            return None
        if isinstance(value, dt.datetime):
            return value.date()
        if isinstance(value, dt.date):
            return value
        try:
            return dt.date.fromisoformat(str(value).strip()[:10])
        except ValueError:
            self.add(f"{where}: '{value}' is not a date (YYYY-MM-DD)")
            return None

    def number(
        self, value: Any, where: str, low: float = 0.0, high: float = math.inf
    ) -> float | None:
        if _blank(value):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            self.add(f"{where}: '{value}' is not a number")
            return None
        if math.isnan(number) or not low <= number <= high:
            self.add(f"{where}: {value} is outside {low:g}..{high:g}")
            return None
        return number

    def flag(self, value: Any, where: str, default: bool) -> bool:
        if _blank(value):
            return default
        text = str(value).strip().lower()
        if text in {"yes", "y", "true", "1", "1.0"}:
            return True
        if text in {"no", "n", "false", "0", "0.0"}:
            return False
        self.add(f"{where}: '{value}' is not yes or no")
        return default


def parse_workdays(text: str) -> frozenset[int] | None:
    """``Mon-Fri``, ``Sun-Thu`` or ``Mon,Tue,Thu`` as weekday numbers (Monday is 0)."""
    days: set[int] = set()
    for part in str(text).lower().replace(" ", "").split(","):
        if not part:
            continue
        ends = part.split("-")
        if len(ends) > 2 or any(end[:3] not in WEEKDAYS or len(end) < 3 for end in ends):
            return None
        first = WEEKDAYS.index(ends[0][:3])
        last = WEEKDAYS.index(ends[-1][:3])
        day = first
        while True:
            days.add(day)
            if day == last:
                break
            day = (day + 1) % 7
    return frozenset(days) or None


def _rows(frame: pd.DataFrame | None) -> Iterable[tuple[int, dict[str, Any]]]:
    """(Excel row number, row as a dict with stripped lower-case keys)."""
    if frame is None:
        return
    columns = [str(c).strip().lower() for c in frame.columns]
    for index, values in enumerate(frame.itertuples(index=False), start=2):
        row = dict(zip(columns, values, strict=True))
        if all(_blank(v) for v in row.values()):
            continue
        yield index, row


def _require(
    frame: pd.DataFrame | None, sheet: str, columns: Sequence[str], errors: Errors
) -> bool:
    if frame is None:
        return False
    present = {str(c).strip().lower() for c in frame.columns}
    missing = [c for c in columns if c not in present]
    if missing:
        errors.add(f"{sheet}: missing columns {missing}")
        return False
    return True


def read_settings(
    frame: pd.DataFrame | None, errors: Errors, overrides: Sequence[tuple[str, str]] = ()
) -> dict[str, Any]:
    """The settings sheet's values, then ``--set`` overrides, over the defaults."""
    settings = dict(DEFAULT_SETTINGS)
    entries: list[tuple[str, Any, str]] = []
    if frame is not None and _require(frame, "settings", ["key", "value"], errors):
        for row, values in _rows(frame):
            key = _text(values["key"]).lower()
            entries.append((key, values["value"], f"settings row {row} ({key})"))
    entries.extend((key.strip().lower(), value, f"--set {key}") for key, value in overrides)
    for key, value, where in entries:
        if key not in DEFAULT_SETTINGS:
            errors.add(f"{where}: unknown setting; known: {sorted(DEFAULT_SETTINGS)}")
        elif _blank(value):
            continue
        elif key in ("iteration_start", "absences_from"):
            settings[key] = errors.date(value, where)
        elif key in ("default_country", "workdays", "vacation_unit"):
            settings[key] = _text(value)
        elif key in ("bug_fix_quota", "focus_loss", "ramp_up_share"):
            settings[key] = errors.number(value, where, 0, 1)
        elif key in ("iteration_weeks", "ramp_up_months"):
            number = errors.number(value, where, 0 if key == "ramp_up_months" else 1, 52)
            if number is not None and number != int(number):
                errors.add(f"{where}: {value} is not a whole number")
            settings[key] = int(number) if number is not None else DEFAULT_SETTINGS[key]
        else:
            settings[key] = errors.number(value, where, 0, 366 if "days" in key else 24)
    start = settings["iteration_start"]
    if start is not None and start.weekday() != 0:
        errors.add(f"settings: iteration_start {start} is not a Monday")
    if settings["vacation_unit"] not in (CALENDAR_DAYS, WORKING_DAYS):
        errors.add(f"settings: vacation_unit must be '{CALENDAR_DAYS}' or '{WORKING_DAYS}'")
    for key in (
        "bug_fix_quota",
        "focus_loss",
        "ramp_up_share",
        "hours_per_day",
        "vacation_days",
        "sick_days",
    ):
        if settings[key] is None:
            settings[key] = DEFAULT_SETTINGS[key]
    if settings["bug_fix_quota"] + settings["focus_loss"] > 1:
        errors.add("settings: bug_fix_quota + focus_loss is more than 1")
    if parse_workdays(settings["workdays"]) is None:
        errors.add(f"settings: workdays '{settings['workdays']}' is not like Mon-Fri")
    return settings


def read_countries(
    frame: pd.DataFrame | None, settings: dict[str, Any], errors: Errors
) -> dict[str, Country]:
    default_workdays = parse_workdays(settings["workdays"]) or frozenset(range(5))
    countries: dict[str, Country] = {}

    def make(name: str, values: dict[str, Any], where: str) -> Country:
        workdays = default_workdays
        if not _blank(values.get("workdays")):
            parsed = parse_workdays(values["workdays"])
            if parsed is None:
                errors.add(f"{where}: workdays '{values['workdays']}' is not like Mon-Fri")
            workdays = parsed or default_workdays
        unit = _text(values.get("vacation_unit")).lower() or settings["vacation_unit"]
        if unit not in (CALENDAR_DAYS, WORKING_DAYS):
            errors.add(f"{where}: vacation_unit must be '{CALENDAR_DAYS}' or '{WORKING_DAYS}'")
        hours = errors.number(values.get("hours_per_day"), f"{where} hours_per_day", 0, 24)
        vacation = errors.number(values.get("vacation_days"), f"{where} vacation_days", 0, 366)
        sick = errors.number(values.get("sick_days"), f"{where} sick_days", 0, 366)
        return Country(
            name,
            workdays,
            settings["hours_per_day"] if hours is None else hours,
            settings["vacation_days"] if vacation is None else vacation,
            unit,
            settings["sick_days"] if sick is None else sick,
        )

    if frame is not None and _require(frame, "countries", ["country"], errors):
        for row, values in _rows(frame):
            name = _text(values["country"])
            where = f"countries row {row}"
            if not name:
                errors.add(f"{where}: blank country")
            elif name in countries:
                errors.add(f"{where}: country '{name}' listed twice")
            else:
                countries[name] = make(name, values, where)
    default = settings["default_country"]
    if default and default not in countries:
        if frame is not None:
            errors.add(f"settings: default_country '{default}' is not in the countries sheet")
        else:
            countries[default] = make(default, {}, "settings")
    return countries


def read_area_countries(
    frame: pd.DataFrame | None, countries: dict[str, Country], errors: Errors
) -> dict[tuple[str, ...], str]:
    result: dict[tuple[str, ...], str] = {}
    if frame is None or not _require(frame, "area_countries", ["area_path", "country"], errors):
        return result
    for row, values in _rows(frame):
        parts = area_parts(_text(values["area_path"]))
        country = _text(values["country"])
        where = f"area_countries row {row}"
        if not parts:
            errors.add(f"{where}: blank area_path")
        elif country not in countries:
            errors.add(f"{where}: unknown country '{country}'")
        elif parts in result:
            errors.add(f"{where}: area_path listed twice")
        else:
            result[parts] = country
    return result


def read_calendar(
    frame: pd.DataFrame | None, countries: dict[str, Country], errors: Errors
) -> dict[tuple[str, dt.date], float]:
    result: dict[tuple[str, dt.date], float] = {}
    if frame is None or not _require(frame, "calendar", ["country", "date", "hours"], errors):
        return result
    for row, values in _rows(frame):
        where = f"calendar row {row}"
        country = _text(values["country"])
        day = errors.date(values["date"], where)
        hours = errors.number(values["hours"], f"{where} hours", 0, 24)
        if country not in countries:
            errors.add(f"{where}: unknown country '{country}'")
        elif day is None or hours is None:
            if day is None and not _blank(values["date"]):
                continue
            errors.add(f"{where}: date and hours are required")
        elif (country, day) in result:
            errors.add(f"{where}: {country} {day} listed twice")
        else:
            result[(country, day)] = hours
    return result


def read_absences(
    frame: pd.DataFrame | None, emails: set[str], errors: Errors
) -> dict[str, list[Absence]]:
    result: dict[str, list[Absence]] = defaultdict(list)
    if frame is None or not _require(frame, "absences", ["email", "from", "to", "type"], errors):
        return result
    for row, values in _rows(frame):
        where = f"absences row {row}"
        email = _text(values["email"]).lower()
        start = errors.date(values["from"], f"{where} from")
        end = errors.date(values["to"], f"{where} to")
        kind = _text(values["type"]).lower()
        hours = errors.number(values.get("hours"), f"{where} hours", 0, 24)
        if email not in emails:
            errors.add(f"{where}: email '{email}' is not in the roster")
        if kind not in ABSENCE_TYPES:
            errors.add(f"{where}: type '{kind}' is not one of {list(ABSENCE_TYPES)}")
        if start is None or end is None:
            if _blank(values["from"]) or _blank(values["to"]):
                errors.add(f"{where}: from and to are required")
            continue
        if end < start:
            errors.add(f"{where}: to {end} is before from {start}")
            continue
        result[email].append(Absence(row, email, start, end, kind, hours))
    return result


def read_iterations(frame: pd.DataFrame | None, errors: Errors) -> list[Iteration] | None:
    if frame is None or not _require(frame, "iterations", ["name", "start", "end"], errors):
        return None
    iterations: list[Iteration] = []
    for row, values in _rows(frame):
        where = f"iterations row {row}"
        name = _text(values["name"])
        start = errors.date(values["start"], f"{where} start")
        end = errors.date(values["end"], f"{where} end")
        if not name:
            errors.add(f"{where}: blank name")
        if start is None or end is None:
            if _blank(values["start"]) or _blank(values["end"]):
                errors.add(f"{where}: start and end are required")
            continue
        if start.weekday() != 0:
            errors.add(f"{where}: start {start} is not a Monday")
        if end < start:
            errors.add(f"{where}: end {end} is before start {start}")
            continue
        iterations.append(Iteration(name, start, end, _text(values.get("phase"))))
    iterations.sort(key=lambda it: it.start)
    for earlier, later in zip(iterations, iterations[1:], strict=False):
        if later.start <= earlier.end:
            errors.add(f"iterations: '{earlier.name}' and '{later.name}' overlap")
    names = [it.name for it in iterations]
    for name in {n for n in names if names.count(n) > 1}:
        errors.add(f"iterations: name '{name}' listed twice")
    return iterations or None


def read_role_groups(
    frame: pd.DataFrame | None, errors: Errors
) -> tuple[dict[str, Group], dict[str, tuple[Group, float]]]:
    groups: dict[str, Group] = {}
    titles: dict[str, tuple[Group, float]] = {}
    if frame is None:
        errors.add("role_groups: sheet is missing")
        return groups, titles
    if not _require(frame, "role_groups", ["title_class", "group"], errors):
        return groups, titles
    for row, values in _rows(frame):
        where = f"role_groups row {row}"
        title = _text(values["title_class"]).lower()
        name = _text(values["group"])
        allocation = errors.number(values.get("allocation"), f"{where} allocation", 0, 1)
        group = Group(
            name,
            errors.flag(values.get("bug_fix"), f"{where} bug_fix", False),
            errors.flag(values.get("triage_pool"), f"{where} triage_pool", False),
        )
        if not title or not name:
            errors.add(f"{where}: title_class and group are required")
            continue
        if title in titles:
            errors.add(f"{where}: title_class '{title}' listed twice")
            continue
        if name in groups and groups[name] != group:
            errors.add(f"{where}: group '{name}' has other bug_fix or triage_pool flags above")
            continue
        groups.setdefault(name, group)
        titles[title] = (groups[name], 1.0 if allocation is None else allocation)
    return groups, titles


def read_rituals(
    frame: pd.DataFrame | None, groups: dict[str, Group], errors: Errors
) -> list[Ritual]:
    rituals: list[Ritual] = []
    if frame is None or not _require(frame, "rituals", ["name", "per", "hours", "groups"], errors):
        return rituals
    for row, values in _rows(frame):
        where = f"rituals row {row}"
        per = _text(values["per"]).lower()
        hours = errors.number(values["hours"], f"{where} hours", 0, 24)
        if per not in (PER_DAY, PER_ITERATION):
            errors.add(f"{where}: per must be '{PER_DAY}' or '{PER_ITERATION}'")
            continue
        names = [n.strip() for n in _text(values["groups"]).split(",") if n.strip()]
        unknown = [n for n in names if n != "all" and n not in groups]
        if unknown:
            errors.add(f"{where}: unknown groups {unknown}")
        if not names:
            errors.add(f"{where}: groups is blank (use 'all' for every group)")
        if hours is None:
            if _blank(values["hours"]):
                errors.add(f"{where}: hours is blank")
            continue
        rituals.append(
            Ritual(_text(values["name"]), per, hours, None if "all" in names else frozenset(names))
        )
    return rituals


def read_rates(frame: pd.DataFrame | None, errors: Errors) -> list[Rate]:
    rates: list[Rate] = []
    if frame is None or not _require(frame, "rates", ["area_path", "from", "to"], errors):
        return rates
    for row, values in _rows(frame):
        where = f"rates row {row}"
        start = errors.date(values["from"], f"{where} from")
        end = errors.date(values["to"], f"{where} to")
        bug = errors.number(values.get("bug_fix_quota"), f"{where} bug_fix_quota", 0, 1)
        focus = errors.number(values.get("focus_loss"), f"{where} focus_loss", 0, 1)
        parts = area_parts(_text(values["area_path"]))
        if not parts:
            errors.add(f"{where}: blank area_path")
            continue
        if start and end and end < start:
            errors.add(f"{where}: to {end} is before from {start}")
            continue
        if bug is not None and focus is not None and bug + focus > 1:
            errors.add(f"{where}: bug_fix_quota + focus_loss is more than 1")
        rates.append(Rate(row, parts, start, end, _text(values.get("phase")), bug, focus))
    for i, first in enumerate(rates):
        for second in rates[i + 1 :]:
            if first.area != second.area or first.start != second.start:
                continue
            if _overlap(first.start, first.end, second.start, second.end):
                errors.add(
                    f"rates rows {first.row} and {second.row}: same AreaPath and start, "
                    "overlapping dates"
                )
    rates.sort(key=lambda r: (-len(r.area), -(r.start or dt.date.min).toordinal()))
    return rates


def _overlap(a: dt.date | None, b: dt.date | None, c: dt.date | None, d: dt.date | None) -> bool:
    return (a or dt.date.min) <= (d or dt.date.max) and (c or dt.date.min) <= (b or dt.date.max)


def read_config(
    book: dict[str, pd.DataFrame],
    roster_emails: set[str],
    overrides: Sequence[tuple[str, str]] = (),
) -> Config:
    """Validate every config sheet; raise one CapacityError listing every error.

    ``overrides`` are ``--set`` (key, value) pairs applied over the settings sheet.
    """
    errors = Errors()
    sheets = {str(name).strip().lower(): frame for name, frame in book.items()}
    settings = read_settings(sheets.get("settings"), errors, overrides)
    countries = read_countries(sheets.get("countries"), settings, errors)
    groups, titles = read_role_groups(sheets.get("role_groups"), errors)
    iterations = read_iterations(sheets.get("iterations"), errors)
    if iterations is None and settings["iteration_start"] is None:
        errors.add("settings: iteration_start is required without an iterations sheet")
    config = Config(
        settings=settings,
        countries=countries,
        area_countries=read_area_countries(sheets.get("area_countries"), countries, errors),
        calendar=read_calendar(sheets.get("calendar"), countries, errors),
        absences=read_absences(sheets.get("absences"), roster_emails, errors),
        iterations=iterations,
        groups=groups,
        title_groups=titles,
        rituals=read_rituals(sheets.get("rituals"), groups, errors),
        rates=read_rates(sheets.get("rates"), errors),
    )
    errors.raise_if_any()
    return config


def is_approved(value: Any) -> bool:
    if isinstance(value, str):
        return value in APPROVED_TEXT
    try:
        return bool(value == 1)
    except (TypeError, ValueError):
        return False


def find_roster_sheet(book: dict[str, pd.DataFrame]) -> str:
    for name, frame in book.items():
        if CLASSIFIED_TITLE in frame.columns and EMAIL in frame.columns:
            return name
    raise CapacityError(f"No roster sheet has '{EMAIL}' and '{CLASSIFIED_TITLE}' columns")


def roster_emails(frame: pd.DataFrame) -> set[str]:
    return {_text(v).lower() for v in frame[EMAIL] if not _blank(v)}


def read_people(frame: pd.DataFrame, config: Config, problems: list[Problem]) -> list[Person]:
    """Approved roster rows as people; rows that cannot be counted become problems."""
    errors = Errors()
    people: list[Person] = []
    seen: set[tuple[str, str]] = set()
    # ado-triage matches AreaPaths case-sensitively: one spelling per AreaPath.
    spellings: dict[tuple[str, ...], str] = {}
    has_approved = APPROVED in frame.columns
    for row, values in _rows(frame):
        if has_approved and not is_approved(values.get(APPROVED)):
            continue
        where = f"roster row {row}"
        email = _text(values.get(EMAIL)).lower()
        area = _text(values.get(AREA_PATH))
        title = _text(values.get(CLASSIFIED_TITLE))
        missing = [
            reason
            for reason, gap in (
                ("no email", not email),
                ("no AreaPath", not area),
                ("no role group", title.lower() not in config.title_groups),
            )
            if gap
        ]
        if missing:
            for reason in missing:
                detail = f"{where}; title '{title}'" if reason == "no role group" else where
                problems.append(Problem(email, area, reason, detail))
            continue
        area = spellings.setdefault(area_parts(area), area)
        if (email, area.lower()) in seen:
            errors.add(f"{where}: {email} is listed twice for {area}")
            continue
        seen.add((email, area.lower()))
        group, default_allocation = config.title_groups[title.lower()]
        country_name = _text(values.get(COUNTRY)) or _area_country(area, config)
        if country_name not in config.countries:
            errors.add(
                f"{where}: unknown country '{country_name}'"
                if country_name
                else f"{where}: no country (set default_country or area_countries)"
            )
            continue
        country = config.countries[country_name]
        allocation = errors.number(values.get(ALLOCATION), f"{where} allocation", 0, 1)
        start = errors.date(values.get(START_DATE), f"{where} start_date")
        end = errors.date(values.get(END_DATE), f"{where} end_date")
        if start and end and end < start:
            errors.add(f"{where}: end_date {end} is before start_date {start}")
        hours = errors.number(values.get(HOURS_PER_DAY), f"{where} hours_per_day", 0, 24)
        vacation = errors.number(values.get(VACATION_DAYS), f"{where} vacation_days", 0, 366)
        sick = errors.number(values.get(SICK_DAYS), f"{where} sick_days", 0, 366)
        ramp_share = errors.number(values.get(RAMP_UP_SHARE), f"{where} ramp_up_share", 0, 1)
        ramp_months = errors.number(values.get(RAMP_UP_MONTHS), f"{where} ramp_up_months", 0, 120)
        people.append(
            Person(
                row=row,
                email=email,
                area_path=area,
                title=title,
                group=group,
                country=country,
                allocation=default_allocation if allocation is None else allocation,
                start=start,
                end=end,
                hours_per_day=hours,
                vacation_days=country.vacation_days if vacation is None else vacation,
                sick_days=country.sick_days if sick is None else sick,
                ramp_share=config.settings["ramp_up_share"] if ramp_share is None else ramp_share,
                ramp_months=int(
                    config.settings["ramp_up_months"] if ramp_months is None else ramp_months
                ),
            )
        )
    errors.raise_if_any()
    totals: dict[str, float] = defaultdict(float)
    for person in people:
        totals[person.email] += person.allocation
    for email, total in totals.items():
        if total > 1 + 1e-9:
            problems.append(Problem(email, "", "allocation over 1", f"{total:g} in total"))
    return people


def _area_country(area_path: str, config: Config) -> str:
    parts = area_parts(area_path)
    for depth in range(len(parts), 0, -1):
        if parts[:depth] in config.area_countries:
            return config.area_countries[parts[:depth]]
    return config.settings["default_country"] or ""


def calendar_problems(
    people: Sequence[Person], config: Config, first: dt.date, last: dt.date
) -> list[Problem]:
    """A country year inside the window with no exception days is probably missing."""
    problems = []
    countries = sorted({p.country.name for p in people})
    for year in range(first.year, last.year + 1):
        for country in countries:
            if not any(c == country and d.year == year for c, d in config.calendar):
                problems.append(
                    Problem(
                        "", "", "no calendar days", f"{country} has no calendar rows for {year}"
                    )
                )
    return problems


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def window(start_month: str, months: int) -> tuple[dt.date, dt.date, list[str]]:
    try:
        first = dt.datetime.strptime(start_month, "%Y-%m").date()
    except ValueError as exc:
        raise CapacityError(f"--start-month '{start_month}' is not YYYY-MM") from exc
    if months < 1:
        raise CapacityError("--months must be at least 1")
    last = add_months(first, months) - dt.timedelta(days=1)
    labels = [add_months(first, i).strftime("%Y-%m") for i in range(months)]
    return first, last, labels


def compute(roster: pd.DataFrame, config: Config, first: dt.date, last: dt.date) -> Result:
    recorded = config.settings["absences_from"]
    if recorded is not None and recorded > first:
        raise CapacityError(
            f"settings: absences_from {recorded} is after the window start {first}; "
            "absences inside the window must be recorded"
        )
    problems: list[Problem] = []
    people = read_people(roster, config, problems)
    problems.extend(calendar_problems(people, config, first, last))
    days = compute_days(people, config, first, last, problems)
    # One problem per person and budget, not one per year and row.
    unique: dict[tuple[str, str, str, str], Problem] = {}
    for problem in problems:
        unique.setdefault(
            (problem.email, problem.area_path, problem.problem, problem.detail), problem
        )
    return Result(days, people, list(unique.values()))


def _country_working_days(
    config: Config, countries: Iterable[str], first: dt.date, last: dt.date
) -> dict[tuple[str, str, str], int]:
    """(country, period type, period) -> the country's working days in it inside the window."""
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for name in set(countries):
        country = config.countries[name]
        for day in _days(first, last):
            hours = config.calendar.get(
                (name, day), country.hours if day.weekday() in country.workdays else 0.0
            )
            if hours <= 0:
                continue
            counts[(name, "month", f"{day.year:04d}-{day.month:02d}")] += 1
            iteration = config.iteration_of(day)
            if iteration is not None:
                counts[(name, "iteration", iteration.name)] += 1
    return counts


def _period_frames(result: Result, config: Config, first: dt.date, last: dt.date) -> pd.DataFrame:
    """Month and iteration rows per person, with FTE."""
    days = result.days
    working = _country_working_days(config, days["country"], first, last)
    frames = []
    keys = ["email", "area_path", "classified_title", "group", "country"]
    for period_type, column in (("month", "month"), ("iteration", "iteration")):
        subset = days[days[column] != ""]
        if subset.empty:
            continue
        weighted = subset.assign(weighted=subset["scale"] * subset["working_day"])
        frame = (
            weighted.groupby([*keys, column], sort=True)[[*COMPONENTS, "weighted"]]
            .sum()
            .reset_index()
        )
        # FTE: allocation x ramp-up averaged over the country's working days in the
        # period, so a joiner in mid-month counts as part of a person.
        denominators = [
            working.get((c, period_type, p), 0)
            for c, p in zip(frame["country"], frame[column], strict=True)
        ]
        frame["fte"] = [
            w / d if d else 0.0 for w, d in zip(frame["weighted"], denominators, strict=True)
        ]
        frame = frame.drop(columns="weighted").rename(columns={column: "period"})
        frame.insert(len(keys), "period_type", period_type)
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=[*keys, "period_type", "period", "fte", *COMPONENTS])
    return pd.concat(frames, ignore_index=True)


def build_outputs(
    result: Result, config: Config, first: dt.date, last: dt.date, months: Sequence[str]
) -> dict[str, pd.DataFrame]:
    by_person = _period_frames(result, config, first, last)
    # An iteration cut by the window edge holds only its days inside the window.
    partial = {
        iteration.name
        for iteration in (config.iteration_of(first), config.iteration_of(last))
        if iteration is not None
        and (iteration.start < first or config.iteration_end(iteration) > last)
    }
    by_person["partial"] = (by_person["period_type"] == "iteration") & by_person["period"].isin(
        partial
    )

    role_keys = ["area_path", "group", "period_type", "period"]
    by_role = (
        by_person.assign(present=by_person["gross"] > 0)
        .groupby(role_keys, sort=True)
        .agg(
            people=("present", "sum"),
            fte=("fte", "sum"),
            partial=("partial", "max"),
            **{c: (c, "sum") for c in COMPONENTS},
        )
        .reset_index()
    )

    pool = {p.area_path for p in result.people if p.group.triage_pool}
    triage_groups = {g.name for g in config.groups.values() if g.triage_pool}
    monthly = by_role[(by_role["period_type"] == "month") & by_role["group"].isin(triage_groups)]
    pivot = (
        monthly.pivot_table(index="area_path", columns="period", values="net", aggfunc="sum")
        .reindex(index=sorted(pool, key=str.lower), columns=list(months))
        .fillna(0.0)
    )
    capacity = pivot.reset_index().rename(columns={"area_path": CAPACITY_AREA_COL})
    capacity.columns.name = None
    if capacity.empty:
        result.problems.append(
            Problem(
                "",
                "",
                "no triage-pool people",
                f"'{CAPACITY_SHEET}' is empty; ado-triage cannot read it",
            )
        )

    problems = pd.DataFrame(
        [vars(p) for p in result.problems], columns=["email", "area_path", "problem", "detail"]
    )
    person_columns = [
        "email",
        "area_path",
        "classified_title",
        "group",
        "country",
        "period_type",
        "period",
        "partial",
        "fte",
        *COMPONENTS,
    ]
    role_columns = [*role_keys, "partial", "people", "fte", *COMPONENTS]
    return {
        CAPACITY_SHEET: capacity,
        "capacity_by_role": by_role[role_columns],
        "capacity_by_person": by_person[person_columns],
        "calendar_weeks": calendar_weeks(result, config, first, last),
        "problems": problems,
    }


def calendar_weeks(result: Result, config: Config, first: dt.date, last: dt.date) -> pd.DataFrame:
    """Working days and hours per country and week, to check against official norms."""
    rows = []
    for name in sorted({p.country.name for p in result.people}):
        country = config.countries[name]
        monday = first - dt.timedelta(days=first.weekday())
        while monday <= last:
            days = [d for d in _days(monday, monday + dt.timedelta(days=6)) if first <= d <= last]
            hours = [
                config.calendar.get(
                    (name, d), country.hours if d.weekday() in country.workdays else 0.0
                )
                for d in days
            ]
            rows.append(
                {
                    "country": name,
                    "week": monday,
                    "working_days": sum(h > 0 for h in hours),
                    "hours": sum(hours),
                }
            )
            monday += dt.timedelta(days=7)
    return pd.DataFrame(rows, columns=["country", "week", "working_days", "hours"])


def template_book() -> dict[str, pd.DataFrame]:
    """A starter config: defaults, the role groups and rituals agreed, UAE 2026-2027."""
    settings = [
        ("iteration_start", dt.date(2026, 1, 5), "Monday the iteration cadence counts from"),
        ("iteration_weeks", 2, "Monday to the next Friday"),
        ("absences_from", None, "absences are recorded from this date; blank: the window start"),
        ("default_country", "UAE", "used when neither the roster nor area_countries sets one"),
        ("workdays", "Mon-Fri", "default week for countries that set none"),
        ("hours_per_day", 8, "default working day"),
        ("vacation_days", 30, "paid leave per year"),
        ("vacation_unit", CALENDAR_DAYS, "calendar or working days"),
        ("sick_days", 7, "working days of sick leave expected per year"),
        ("bug_fix_quota", 0.5, "share for groups with bug_fix = yes"),
        ("focus_loss", 0.12, "interrupts and context switching"),
        ("ramp_up_share", 0.5, "a joiner's share of full speed"),
        ("ramp_up_months", 3, "months after start_date at that share"),
    ]
    groups = [
        ("developer", "developer", 1.0, "yes", "yes"),
        ("lead developer", "developer", 1.0, "yes", "yes"),
        ("research developer", "developer", 1.0, "yes", "yes"),
        ("team lead", "team lead", 0.5, "yes", "yes"),
        ("testing engineer", "test", 1.0, "no", "no"),
        ("sdet", "test", 1.0, "no", "no"),
        ("architect", "architect", 1.0, "no", "no"),
        ("analyst", "analyst", 1.0, "no", "no"),
        ("security analyst", "analyst", 1.0, "no", "no"),
        ("manager", "management", 0.0, "no", "no"),
        ("head", "management", 0.0, "no", "no"),
    ]
    rituals = [
        ("daily scrum", PER_DAY, 0.5, "developer, team lead, test, analyst"),
        ("code review", PER_DAY, 1.0, "developer, team lead"),
        ("pre-planning", PER_ITERATION, 1.0, "developer, team lead, test, analyst"),
        ("demo", PER_ITERATION, 1.0, "developer, team lead, test, architect, analyst"),
        ("retro", PER_ITERATION, 1.0, "developer, team lead, test"),
        ("1 on 1", PER_ITERATION, 0.5, "developer, team lead, test"),
    ]
    return {
        "settings": pd.DataFrame(settings, columns=["key", "value", "note"]),
        "countries": pd.DataFrame(
            [("UAE", "Mon-Fri", 8, 30, CALENDAR_DAYS, 7)],
            columns=[
                "country",
                "workdays",
                "hours_per_day",
                "vacation_days",
                "vacation_unit",
                "sick_days",
            ],
        ),
        "area_countries": pd.DataFrame(columns=["area_path", "country"]),
        "calendar": pd.DataFrame(
            uae_calendar_rows(), columns=["country", "date", "hours", "name", "status"]
        ),
        "absences": pd.DataFrame(columns=["email", "from", "to", "type", "hours"]),
        "role_groups": pd.DataFrame(
            groups, columns=["title_class", "group", "allocation", "bug_fix", "triage_pool"]
        ),
        "rituals": pd.DataFrame(rituals, columns=["name", "per", "hours", "groups"]),
        "rates": pd.DataFrame(
            columns=["area_path", "from", "to", "phase", "bug_fix_quota", "focus_loss"]
        ),
        "iterations": pd.DataFrame(columns=["name", "start", "end", "phase"]),
    }


# ---------------------------------------------------------------------------
# Excel IO
# ---------------------------------------------------------------------------

MAX_COLUMN_WIDTH = 50
DATE_FORMAT = "YYYY-MM-DD"
HOUR_COLUMNS = {*COMPONENTS, "fte", "scale"}


def read_book(path: Path, what: str) -> dict[str, pd.DataFrame]:
    if not path.exists():
        raise CapacityError(f"{what} does not exist: {path}")
    try:
        return pd.read_excel(path, sheet_name=None, engine="openpyxl")
    except Exception as exc:  # openpyxl raises many types for unreadable files
        raise CapacityError(f"Cannot read {what} {path}: {exc}") from exc


CONFIG_SHEETS = (
    "settings",
    "countries",
    "area_countries",
    "calendar",
    "absences",
    "iterations",
    "role_groups",
    "rituals",
    "rates",
)


CSV_SEPARATORS = (",", ";", "\t")


def read_table(path: Path, sheet: str) -> pd.DataFrame:
    """One config sheet from a CSV file, or from a workbook's sheet of that name (or
    its only sheet), with stripped lower-case column names.

    CSV cells are read as text. The separator is whichever of ``,``, ``;`` or tab
    the header line holds most often; a header without any is one column.
    """
    if not path.exists():
        raise CapacityError(f"{sheet} file does not exist: {path}")
    try:
        if path.suffix.lower() in (".csv", ".txt"):
            with path.open(encoding="utf-8-sig") as handle:
                header = handle.readline()
            separator = max(CSV_SEPARATORS, key=header.count)
            frame = pd.read_csv(
                path,
                sep=separator if header.count(separator) else ",",
                dtype=str,
                keep_default_na=False,
                encoding="utf-8-sig",
            )
        else:
            book = pd.read_excel(path, sheet_name=None, engine="openpyxl")
            named = {str(name).strip().lower(): f for name, f in book.items()}
            if sheet in named:
                frame = named[sheet]
            elif len(book) == 1:
                frame = next(iter(book.values()))
            else:
                raise CapacityError(f"{path} has several sheets and none is named {sheet}")
    except CapacityError:
        raise
    except Exception as exc:  # pandas and openpyxl raise many types for unreadable files
        raise CapacityError(f"Cannot read {sheet} file {path}: {exc}") from exc
    # Stacked files align by column name, so names are normalised before stacking.
    columns = [str(c).strip().lower() for c in frame.columns]
    repeated = sorted({c for c in columns if columns.count(c) > 1})
    if repeated:
        raise CapacityError(f"{path}: columns {repeated} appear more than once")
    frame.columns = columns
    return frame


def parse_assignment(text: str, flag: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key.strip():
        raise CapacityError(f"{flag} '{text}' is not KEY=VALUE")
    return key.strip(), value.strip()


def external_sheets(
    calendars: Sequence[Path], sheets: Sequence[str]
) -> dict[str, tuple[pd.DataFrame, list[Path]]]:
    """Config sheets given as files: ``--calendar FILE`` and ``--sheet NAME=FILE``.

    Several files for one sheet are stacked (a calendar per year, say); together they
    replace that sheet of the config workbook. Returns each sheet with its files.
    """
    files: dict[str, list[Path]] = defaultdict(list)
    for path in calendars:
        files["calendar"].append(path)
    for text in sheets:
        name, value = parse_assignment(text, "--sheet")
        name = name.lower()
        if name not in CONFIG_SHEETS:
            raise CapacityError(f"--sheet {name}: unknown sheet; known: {list(CONFIG_SHEETS)}")
        if not value:
            raise CapacityError(f"--sheet {name}: no file given")
        files[name].append(Path(value))
    return {
        name: (pd.concat([read_table(p, name) for p in paths], ignore_index=True), paths)
        for name, paths in files.items()
    }


def load_config(args: argparse.Namespace, roster_emails: set[str]) -> tuple[Config, list[Path]]:
    """The config workbook with the command line's sheet files and ``--set`` applied."""
    book = read_book(args.config, "config")
    lower = {str(name).strip().lower(): name for name in book}
    files: list[Path] = []
    for name, (frame, paths) in external_sheets(args.calendar, args.sheet).items():
        book.pop(lower.get(name, name), None)
        book[name] = frame
        files.extend(paths)
        logger.info("Sheet %s from %s", name, ", ".join(str(p) for p in paths))
    overrides = [parse_assignment(text, "--set") for text in args.set]
    return read_config(book, roster_emails, overrides), files


def add_config_arguments(
    root: argparse.ArgumentParser, required: bool = False, config_help: str = ""
) -> None:
    """``--config`` and the flags that replace its sheets or override its settings."""
    root.add_argument(
        "--config",
        type=Path,
        required=required,
        help=config_help or "config workbook (see --template)",
    )
    root.add_argument(
        "--calendar",
        type=Path,
        action="append",
        default=[],
        metavar="FILE",
        help="calendar sheet as .xlsx or .csv (country, date, hours); repeat for one "
        "file per year; replaces the config's calendar sheet",
    )
    root.add_argument(
        "--sheet",
        action="append",
        default=[],
        metavar="NAME=FILE",
        help=f"replace a config sheet with a .xlsx or .csv file; NAME is one of "
        f"{', '.join(CONFIG_SHEETS)}; repeat a NAME to stack files",
    )
    root.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a setting of the settings sheet, e.g. --set bug_fix_quota=0.4",
    )


def write_book(book: dict[str, pd.DataFrame], path: Path) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, frame in book.items():
            frame = excel_safe(frame)
            frame.to_excel(writer, sheet_name=name, index=False)
            sheet = writer.sheets[name]
            keep_text_literal(sheet, min_row=1)
            for position, heading in enumerate(frame.columns, start=1):
                series = frame.iloc[:, position - 1]
                lengths = [len(_display(v)) for v in series if not _blank(v)]
                width = min(max([*lengths, len(str(heading))]) + 2, MAX_COLUMN_WIDTH)
                letter = get_column_letter(position)
                sheet.column_dimensions[letter].width = width
                hours = heading in HOUR_COLUMNS or (name == CAPACITY_SHEET and position > 1)
                for cell in sheet[letter][1:]:
                    if hours:
                        cell.number_format = "0.00"
                    elif isinstance(cell.value, dt.datetime):
                        cell.number_format = DATE_FORMAT  # dates carry no time of day


def _display(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, dt.datetime | pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    return str(value)


def excel_dates(frame: pd.DataFrame) -> pd.DataFrame:
    """Dates as Excel dates: pandas writes ``datetime.date`` objects as text otherwise."""
    frame = frame.copy()
    for column in frame.columns:
        values = frame[column].dropna()
        if len(values) and values.map(lambda v: isinstance(v, dt.date)).all():
            frame[column] = pd.to_datetime(frame[column])
        elif values.map(lambda v: isinstance(v, dt.date)).any():
            frame[column] = (
                frame[column]
                .map(lambda v: pd.Timestamp(v) if isinstance(v, dt.date) else v)
                .astype(object)
            )
    return frame


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def write_sheets(directory: Path, overwrite: bool) -> list[Path]:
    """Every sheet of the starter config as ``<sheet>.csv`` in ``directory``, to edit
    and pass with ``--sheet NAME=FILE`` (``--calendar`` for the calendar)."""

    def text(value: Any) -> Any:
        return value.isoformat() if isinstance(value, dt.date) else value

    targets = []
    for name, frame in template_book().items():
        frame = frame.apply(lambda column: column.map(text)) if len(frame) else frame

        def write(tmp: Path, frame: pd.DataFrame = frame) -> None:
            frame.to_csv(tmp, index=False, encoding="utf-8")

        targets.append((directory / f"{name}.csv", write))
    directory.mkdir(parents=True, exist_ok=True)
    publish_all(targets, overwrite=overwrite, exists_error=CapacityError, exclusive=True)
    return [path for path, _ in targets]


def run(args: argparse.Namespace) -> int:
    if args.template_sheets:
        paths = write_sheets(args.template_sheets, args.overwrite)
        logger.info("Wrote %s", ", ".join(str(p) for p in paths))
        if not args.template:
            return 0
    if args.template:
        output = args.template
        publish_all(
            [
                (
                    output,
                    lambda tmp: write_book(
                        {k: excel_dates(v) for k, v in template_book().items()}, tmp
                    ),
                )
            ],
            overwrite=args.overwrite,
            exists_error=CapacityError,
            exclusive=True,
        )
        logger.info("Wrote a starter config to %s", output)
        return 0
    missing = [
        flag
        for flag, value in (
            ("--roster", args.roster),
            ("--config", args.config),
            ("--start-month", args.start_month),
            ("--output", args.output),
        )
        if value is None
    ]
    if missing:
        raise CapacityError(f"Missing {', '.join(missing)} (or use --template)")
    if args.output.exists() and not args.overwrite:
        raise CapacityError(f"{args.output} exists; pass --overwrite to replace it")

    first, last, months = window(args.start_month, args.months)
    roster_book = read_book(args.roster, "roster")
    roster = roster_book[find_roster_sheet(roster_book)]
    config, files = load_config(args, roster_emails(roster))
    inputs = {path.resolve() for path in (args.roster, args.config, *files)}
    if args.output.resolve() in inputs:
        raise CapacityError("--output may not be an input file")
    result = compute(roster, config, first, last)
    outputs = build_outputs(result, config, first, last, months)
    publish_all(
        [
            (
                args.output,
                lambda tmp: write_book({k: excel_dates(v) for k, v in outputs.items()}, tmp),
            )
        ],
        overwrite=args.overwrite,
        exists_error=CapacityError,
        exclusive=True,
    )
    logger.info(
        "Wrote %s: %d people, %d AreaPaths, months %s..%s",
        args.output,
        len(result.people),
        len(outputs[CAPACITY_SHEET]),
        months[0],
        months[-1],
    )
    if result.problems:
        for problem in result.problems:
            logger.warning(
                "Problem: %s %s %s %s",
                problem.problem,
                problem.email,
                problem.area_path,
                problem.detail,
            )
        if not args.allow_problems:
            logger.error(
                "%d problems; see the problems sheet (or pass --allow-problems)",
                len(result.problems),
            )
            return 3
    return 0


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"'{text}' is not a whole number of at least 1")
    return value


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --template capacity-config.xlsx
  %(prog)s --roster roster.xlsx --config capacity-config.xlsx --start-month 2026-11 \\
      --months 3 --output capacity.xlsx
  %(prog)s --roster roster.xlsx --config capacity-config.xlsx --start-month 2026-11 \\
      --calendar holidays-2026.csv --calendar holidays-2027.xlsx \\
      --set bug_fix_quota=0.4 --sheet absences=leave.csv --output capacity.xlsx
""",
    )
    root.add_argument("--roster", type=Path, help="team-roster output (one row per person)")
    add_config_arguments(root)
    root.add_argument("--start-month", help="first month of the window, YYYY-MM")
    root.add_argument(
        "--months", type=_positive, default=3, help="months in the window (default 3)"
    )
    root.add_argument("--output", type=Path, help="output workbook")
    root.add_argument("--overwrite", action="store_true", help="replace an existing output")
    root.add_argument(
        "--allow-problems",
        action="store_true",
        help="exit 0 even when the problems sheet is not empty",
    )
    root.add_argument(
        "--template",
        type=Path,
        metavar="CONFIG",
        help="write a starter config with UAE 2026-2027 calendar and exit",
    )
    root.add_argument(
        "--template-sheets",
        type=Path,
        metavar="DIR",
        help="write every sheet of the starter config as DIR/<sheet>.csv (the names "
        "--sheet takes) and exit",
    )
    root.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", stream=sys.stderr)
    logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    try:
        return run(args)
    except CapacityError as exc:
        logger.error("%s", exc)
        return 1
    except OSError as exc:
        logger.error("Cannot write output: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
