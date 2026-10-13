"""Validate Power Planner quantities, intervals, and calendar metrics."""

from __future__ import annotations

import math
import re
from calendar import monthrange
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import cast

from .exceptions import KepcoOnProtocolError

NUMBER = re.compile(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?")


def planner_quantity(value: object) -> float | None:
    """Accept finite nonnegative quantities with explicit comma grouping."""
    if value is None or (isinstance(value, str) and value.strip().lower() in {"", "null"}):
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise KepcoOnProtocolError("Invalid Power Planner quantity")
    normalized = str(value).strip()
    if NUMBER.fullmatch(normalized) is None:
        raise KepcoOnProtocolError("Invalid Power Planner quantity")
    try:
        parsed = float(normalized.replace(",", ""))
    except ValueError, OverflowError:
        raise KepcoOnProtocolError("Invalid Power Planner quantity") from None
    if not math.isfinite(parsed):
        raise KepcoOnProtocolError("Invalid Power Planner quantity")
    return parsed


def planner_date(value: object) -> date:
    """Read the verified full dates without borrowing a bill's period."""
    if not isinstance(value, str):
        raise KepcoOnProtocolError("Invalid Power Planner period")
    normalized = value.strip().replace(".", "-")
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", normalized) is None:
        raise KepcoOnProtocolError("Invalid Power Planner period")
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        raise KepcoOnProtocolError("Invalid Power Planner period") from None


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise KepcoOnProtocolError("Unexpected Power Planner object")
    return cast("dict[str, object]", value)


def _rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise KepcoOnProtocolError("Unexpected Power Planner list")
    return [_object(item) for item in value]


def _hour(row: dict[str, object]) -> int:
    label = row.get("MR_HHMI")
    if not isinstance(label, str) or re.fullmatch(r"[0-9]{2}시", label) is None:
        raise KepcoOnProtocolError("Invalid Power Planner interval label")
    hour = int(label[:2])
    if hour > 24:
        raise KepcoOnProtocolError("Invalid Power Planner interval label")
    return hour


@dataclass(frozen=True, slots=True)
class DailyUsage:
    quantity: float | None
    recent: float | None
    intervals: tuple[tuple[int, float], ...]
    status: str

    @property
    def count(self) -> int:
        return len(self.intervals)


def parse_daily(
    total: object, chart: object, *, completed: bool
) -> tuple[float | None, float | None, int, str]:
    """Preserve the original public contract while sharing validated intervals."""
    result = parse_daily_metrics(total, chart, completed=completed)
    return result.quantity, result.recent, result.count, result.status


def parse_daily_metrics(total: object, chart: object, *, completed: bool) -> DailyUsage:
    """Honor missing markers before numbers and require complete past days."""
    totals, intervals = _rows(total), _rows(chart)
    if len(totals) != 1 or len(intervals) != 24:
        raise KepcoOnProtocolError("Invalid Power Planner daily range")
    valid: list[tuple[int, float]] = []
    hours: set[int] = set()
    for row in intervals:
        hour = _hour(row)
        if hour in hours:
            raise KepcoOnProtocolError("Duplicate Power Planner interval")
        hours.add(hour)
        marker = row.get("NO_DATA_YN")
        if marker == "Y":
            continue
        if marker != "N":
            raise KepcoOnProtocolError("Invalid Power Planner missing marker")
        value = planner_quantity(row.get("F_AP_QT"))
        if value is not None:
            valid.append((hour, value))
    if hours not in (set(range(24)), set(range(1, 25))):
        raise KepcoOnProtocolError("Invalid Power Planner hourly range")
    if not valid:
        return DailyUsage(None, None, (), "no_data")
    recent = max(valid)[1]
    if completed and len(valid) != 24:
        return DailyUsage(None, recent, tuple(sorted(valid)), "incomplete")
    quantity = planner_quantity(totals[0].get("F_AP_QT"))
    status = "no_data" if quantity is None else "ok" if len(valid) == 24 else "partial"
    return DailyUsage(quantity, recent, tuple(sorted(valid)), status)


class PlannerMetricError(KepcoOnProtocolError):
    """Carry a controlled status without retaining a provider response."""

    def __init__(self, status: str) -> None:
        super().__init__("Power Planner metric validation failed")
        self.status = status


@dataclass(frozen=True, slots=True)
class CalendarDay:
    day: date
    usage: float | None
    prior_year_usage: float | None
    prior_year_status: str = "ok"


def parse_calendar_month(value: object, year: int, month: int) -> tuple[CalendarDay, ...]:
    """Bind the verified MM.DD labels to the explicitly requested calendar month."""
    rows = _rows(value)
    if len(rows) != monthrange(year, month)[1]:
        raise PlannerMetricError("incomplete")
    days: dict[date, CalendarDay] = {}
    for row in rows:
        label = row.get("MR_YMD2")
        match = re.fullmatch(r"([0-9]{2})\.([0-9]{2})", label) if isinstance(label, str) else None
        if match is None or int(match[1]) != month:
            raise PlannerMetricError("period_mismatch")
        try:
            day = date(year, month, int(match[2]))
        except ValueError:
            raise KepcoOnProtocolError("Invalid planner calendar date") from None
        if day in days:
            raise KepcoOnProtocolError("Duplicate planner calendar date")
        marker = row.get("NO_DATA_YN")
        if marker not in {None, "N", "Y"}:
            raise KepcoOnProtocolError("Invalid planner calendar missing marker")
        quantity = None if marker == "Y" else planner_quantity(row.get("F_AP_QT"))
        try:
            previous = planner_quantity(row.get("LYEAR_F_AP_QT"))
            previous_status = "ok" if previous is not None else "no_data"
        except KepcoOnProtocolError:
            previous, previous_status = None, "invalid_response"
        days[day] = CalendarDay(day, quantity, previous, previous_status)
    return tuple(days[day] for day in sorted(days))


def calendar_total(rows: tuple[CalendarDay, ...], through: date) -> float:
    """Exclude future placeholders and reject missing actual-period rows."""
    values: list[float] = []
    for row in rows:
        if row.day > through:
            if row.usage not in {None, 0}:
                raise PlannerMetricError("period_mismatch")
            continue
        if row.usage is None:
            raise PlannerMetricError("incomplete")
        values.append(row.usage)
    return sum_quantities(values)


def check_calendar_total(value: object, total: float, count: int) -> None:
    rows = _rows(value)
    if len(rows) != 1:
        raise KepcoOnProtocolError("Invalid planner calendar aggregate")
    supplied = planner_quantity(rows[0].get("F_AP_QT"))
    if supplied is None:
        raise PlannerMetricError("no_data")
    if not math.isclose(supplied, total, rel_tol=0, abs_tol=max(0.02, count * 0.01)):
        raise KepcoOnProtocolError("Planner calendar aggregate does not match rows")


def planner_level(value: object) -> int | None:
    quantity = planner_quantity(value)
    if quantity is None:
        return None
    if quantity not in {1, 2, 3}:
        raise PlannerMetricError("unsupported_profile")
    return int(quantity)


class ComparisonDefaults(HTMLParser):
    """Read the same server-rendered select defaults used by the portal request."""

    fields = frozenset({"year", "month", "SIDO_CD", "SIGUNGU_CD", "DONGCD"})

    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, str] = {}
        self._select: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "select":
            name = attributes.get("id") or attributes.get("name")
            self._select = name if name in self.fields else None
        elif tag == "option" and self._select is not None:
            value = attributes.get("value", "") or ""
            if self._select not in self.values or "selected" in attributes:
                self.values[self._select] = value

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._select = None

    def validated(self, today: date) -> dict[str, str]:
        if set(self.values) != self.fields:
            raise PlannerMetricError("comparison_scope_unverified")
        year, month = self.values["year"], self.values["month"]
        if re.fullmatch(r"[0-9]{4}", year) is None or re.fullmatch(r"[0-9]{1,2}", month) is None:
            raise PlannerMetricError("comparison_scope_unverified")
        try:
            period = date(int(year), int(month), 1)
        except ValueError:
            raise PlannerMetricError("comparison_scope_unverified") from None
        if period > today.replace(day=1):
            raise PlannerMetricError("period_mismatch")
        if any(
            re.fullmatch(r"[A-Za-z0-9_-]{0,16}", self.values[key]) is None
            for key in self.fields - {"year", "month"}
        ):
            raise PlannerMetricError("comparison_scope_unverified")
        return dict(self.values)


def parse_neighbors(value: object) -> tuple[dict[str, float | None], dict[str, str]]:
    """Honor AA's usage/amount roles even though both rows share F_AP_QT names."""
    rows = _rows(value)
    if not rows:
        raise PlannerMetricError("no_data")
    if len(rows) != 2:
        raise KepcoOnProtocolError("Ambiguous planner comparison rows")
    values: dict[str, float | None] = {}
    roles: set[str] = set()
    statuses: dict[str, str] = {}
    for row in rows:
        label = row.get("AA")
        if not isinstance(label, str):
            raise KepcoOnProtocolError("Missing planner comparison role")
        usage, amount = "사용량" in label, "요금" in label
        if usage == amount:
            raise KepcoOnProtocolError("Unknown planner comparison role")
        role = "usage" if usage else "amount"
        if role in roles:
            raise KepcoOnProtocolError("Duplicate planner comparison role")
        roles.add(role)
        suffix = "kwh" if usage else "krw"
        for field, source in (
            (f"regional_average_{role}_{suffix}", "F_AP_QT_ALL_DISP"),
            (f"saving_neighbor_{role}_{suffix}", "F_AP_QT_SAVE_DISP"),
            (f"regional_{role}_percentile_pct", "RANK_RATE"),
        ):
            try:
                quantity = planner_quantity(row.get(source))
                if source == "RANK_RATE" and quantity is not None and quantity > 100:
                    raise KepcoOnProtocolError("Invalid planner comparison percentile")
                values[field] = quantity
                statuses[field] = "ok" if quantity is not None else "no_data"
            except KepcoOnProtocolError:
                values[field] = None
                statuses[field] = "invalid_response"
    return values, statuses


def sum_quantities(values: Iterable[float]) -> float:
    try:
        result = math.fsum(values)
    except OverflowError:
        raise KepcoOnProtocolError("Planner quantity sum overflow") from None
    if not math.isfinite(result):
        raise KepcoOnProtocolError("Planner quantity sum is not finite")
    return result
