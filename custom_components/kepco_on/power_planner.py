"""Verified Power Planner authentication, session storage, and read contracts."""

from __future__ import annotations

import asyncio
import json
import math
import re
from calendar import monthrange
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Protocol, cast
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from aiohttp import ClientError, ClientSession, ClientTimeout, CookieJar
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from yarl import URL

from .api import USER_AGENT, _read_bounded_body
from .const import DOMAIN
from .exceptions import (
    KepcoOnAuthError,
    KepcoOnConnectionError,
    KepcoOnNoCustomersError,
    KepcoOnProtocolError,
    KepcoOnRateLimitError,
    KepcoOnSessionExpired,
    KepcoOnUnsupportedAccount,
)
from .models import PowerPlannerSnapshot, PowerPlannerSourceMetadata
from .power_planner_parser import (
    CalendarDay,
    ComparisonDefaults,
    DailyUsage,
    PlannerMetricError,
    _rows,
    calendar_total,
    check_calendar_total,
    parse_calendar_month,
    parse_daily_metrics,
    parse_neighbors,
    planner_level,
    sum_quantities,
)
from .power_planner_parser import (
    _object as _object,
)
from .power_planner_parser import (
    parse_daily as parse_daily,
)
from .power_planner_parser import (
    planner_date as planner_date,
)
from .power_planner_parser import (
    planner_quantity as planner_quantity,
)

ORIGIN = URL("https://pp.kepco.co.kr")
LANDING = "/rm/rm0201.do"
PATHS = frozenset(
    {
        "/intro.do",
        "/intro/chkUser.do",
        "/login",
        LANDING,
        "/auth/refreshToken.do",
        "/auth/usercustno_list.do",
        "/rm/rm0101_contract_info.do",
        "/rm/getRM0201.do",
        "/rs/rs0201_list.do",
        "/rs/rs0201_chart.do",
        "/rs/rs0202_list.do",
        "/rs/rs0202_chart.do",
        "/re/re0202_list.do",
        "/cc/cc0202.do",
        "/cc/cc0202Info.do",
    }
)
# RFC 6265 cookie-octet: opaque session cookies need not be alphanumeric.
SESSION_ID = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,1024}")
KOREA = ZoneInfo("Asia/Seoul")


METRIC_FIELDS = {
    "tier": ("current_bill_level",),
    "month": ("calendar_month_usage_kwh",),
    "year": ("calendar_year_usage_kwh",),
    "day_comparison": (
        "prior_year_same_day_usage_kwh",
        "same_day_year_on_year_usage_difference_kwh",
        "same_day_year_on_year_usage_ratio_pct",
    ),
    "neighbors": (
        "regional_average_usage_kwh",
        "saving_neighbor_usage_kwh",
        "regional_usage_percentile_pct",
        "regional_average_amount_krw",
        "saving_neighbor_amount_krw",
        "regional_amount_percentile_pct",
    ),
}


@dataclass(frozen=True, slots=True)
class MetricResult:
    values: dict[str, float | int | None]
    metadata: PowerPlannerSourceMetadata
    statuses: dict[str, str]


@dataclass(frozen=True, slots=True)
class MetricCache:
    key: tuple[object, ...]
    attempted_at: datetime
    result: MetricResult


class PlannerStore(Protocol):
    """Persist only the provider's validated session identifier."""

    async def async_load(self) -> str | None: ...
    async def async_save(self, session_id: str) -> None: ...
    async def async_clear(self) -> None: ...


class PowerPlannerSessionStore:
    """Separate the planner cookie from KEPCO ON's tokens and cookies."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, str]] = Store(hass, 1, f"{DOMAIN}.power_planner.{entry_id}")

    async def async_load(self) -> str | None:
        payload = await self._store.async_load()
        if payload is None:
            return None
        value = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(value, str) or SESSION_ID.fullmatch(value) is None:
            raise KepcoOnProtocolError("Invalid stored Power Planner session")
        return value

    async def async_save(self, session_id: str) -> None:
        if SESSION_ID.fullmatch(session_id) is None:
            raise KepcoOnProtocolError("Invalid Power Planner session identifier")
        await self._store.async_save({"session_id": session_id})

    async def async_clear(self) -> None:
        await self._store.async_remove()


def decode_cookie(value: str) -> str:
    """Match decodeURIComponent, preserving a decoded literal plus sign."""
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise KepcoOnProtocolError("Invalid Power Planner cookie encoding")
    try:
        return unquote(value, encoding="utf-8", errors="strict")
    except UnicodeError:
        raise KepcoOnProtocolError("Invalid Power Planner cookie encoding") from None


def _authenticated(body: bytes) -> bool:
    text = body.decode("utf-8", errors="replace")
    return (
        re.search(r"id=[\"']RSA_USER_(?:ID|PWD)[\"']", text) is None
        and re.search(r"logout|로그아웃", text, re.IGNORECASE) is not None
        and re.search(r"스마트뷰|실시간사용량|전기사용량", text) is not None
    )


class PowerPlannerClient:
    """A fixed-origin client for the verified main customer and hourly profile."""

    def __init__(
        self,
        session: ClientSession,
        store: PlannerStore | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(KOREA),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not isinstance(session.cookie_jar, CookieJar) or session.cookie_jar.quote_cookie:
            raise ValueError("Power Planner requires CookieJar(quote_cookie=False)")
        self.session = session
        self._store = store
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._authenticated = False
        self._stored_id: str | None = None
        self.main_customer_number: str | None = None
        self._snapshot: PowerPlannerSnapshot | None = None
        self._yesterday: tuple[date, float | None, int, str] | None = None
        self._yesterday_fetched_at: datetime | None = None
        self._contract: dict[str, object] = {}
        self._metrics: dict[str, MetricCache] = {}
        self._months: dict[tuple[int, int], tuple[datetime, tuple[CalendarDay, ...]]] = {}
        self._blocked_until: datetime | None = None

    async def _request(
        self, method: str, path: str, *, payload: object = None, form: dict[str, str] | None = None
    ) -> tuple[bytes, str | None]:
        if path not in PATHS or method not in {"GET", "POST"}:
            raise KepcoOnProtocolError("Power Planner request path is not allowed")
        if self._blocked_until is not None and self._clock() < self._blocked_until:
            raise KepcoOnRateLimitError("Power Planner rate limited")
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": str(ORIGIN / "intro.do"),
            "Origin": str(ORIGIN),
        }
        for attempt in range(3):
            try:
                async with self.session.request(
                    method,
                    ORIGIN / path.lstrip("/"),
                    json=payload if form is None and method == "POST" else None,
                    data=form,
                    headers=headers,
                    timeout=ClientTimeout(total=30),
                    allow_redirects=False,
                ) as response:
                    if response.url.origin() != ORIGIN:
                        raise KepcoOnProtocolError("Power Planner response origin changed")
                    if response.status == 429:
                        self._blocked_until = self._clock() + timedelta(minutes=20)
                        raise KepcoOnRateLimitError("Power Planner rate limited")
                    if response.status in {401, 403}:
                        raise KepcoOnSessionExpired("Power Planner session expired")
                    if (
                        response.status in {500, 502, 503}
                        and attempt < 2
                        and path not in {"/login", "/intro/chkUser.do"}
                    ):
                        await self._sleep(float(2**attempt))
                        continue
                    if response.status in {301, 302, 303, 307, 308}:
                        if path != "/login" or response.status not in {302, 303}:
                            raise KepcoOnSessionExpired("Power Planner session expired")
                        location = response.headers.get("Location")
                        if location is None:
                            raise KepcoOnProtocolError("Power Planner login redirect missing")
                        target = response.url.join(URL(location))
                        if (
                            target.origin() != ORIGIN
                            or target.path != LANDING
                            or set(target.query) - {"menu_id"}
                        ):
                            raise KepcoOnAuthError("Power Planner login was not accepted")
                        # Ignore the site's navigation parameter; visit only the fixed landing.
                        return b"", LANDING
                    if response.status != 200:
                        raise KepcoOnConnectionError("Power Planner HTTP failure")
                    return await _read_bounded_body(response), None
            except TimeoutError, ClientError:
                if attempt == 2 or path in {"/login", "/intro/chkUser.do"}:
                    raise KepcoOnConnectionError("Power Planner connection failed") from None
                await self._sleep(float(2**attempt))
        raise KepcoOnConnectionError("Power Planner connection failed")

    async def _json(self, path: str, payload: dict[str, object]) -> object:
        body, _ = await self._request("POST", path, payload=payload)
        return self._decode_json(body)

    async def _form_json(self, path: str, payload: dict[str, str]) -> object:
        body, _ = await self._request("POST", path, form=payload)
        return self._decode_json(body)

    @staticmethod
    def _decode_json(body: bytes) -> object:
        if b"RSA_USER_ID" in body:
            raise KepcoOnSessionExpired("Power Planner session expired")
        try:
            return cast("object", json.loads(body))
        except ValueError, UnicodeError:
            pass
        raise KepcoOnProtocolError("Unexpected Power Planner JSON")

    async def _persist(self) -> None:
        cookie = self.session.cookie_jar.filter_cookies(ORIGIN).get("JSESSIONID")
        if cookie is None or SESSION_ID.fullmatch(cookie.value) is None:
            raise KepcoOnProtocolError("Power Planner authentication cookie missing")
        if self._store is not None and cookie.value != self._stored_id:
            await self._store.async_save(cookie.value)
            self._stored_id = cookie.value

    async def async_login(self, username: str, password: str) -> None:
        """Use the site's RSA wire format; retain no plaintext credentials."""
        if not username.strip() or not password:
            raise KepcoOnAuthError("Power Planner credentials required")
        self._authenticated = False
        self._stored_id = None
        self.session.cookie_jar.clear()
        body, _ = await self._request("GET", "/intro.do")
        cookies = self.session.cookie_jar.filter_cookies(ORIGIN)
        try:
            prefix = decode_cookie(cookies["cookieSsId"].value)
            modulus = decode_cookie(cookies["cookieRsa"].value)
            exponent = re.search(
                rb'id=["\']RSAExponent["\'][^>]*value=["\']([0-9a-fA-F]+)["\']', body
            )
            if exponent is None:
                raise ValueError
            key = rsa.RSAPublicNumbers(int(exponent[1], 16), int(modulus, 16)).public_key()

            def encrypt(value: str) -> str:
                ciphertext = (
                    key.encrypt(value.encode("utf-8"), padding.PKCS1v15()).hex().lstrip("0")
                )
                return f"{prefix}_{ciphertext if len(ciphertext) % 2 == 0 else '0' + ciphertext}"

            encrypted_id, encrypted_password = encrypt(username.strip()), encrypt(password)
        except KeyError, ValueError:
            raise KepcoOnProtocolError("Unexpected Power Planner RSA bootstrap") from None
        result = _object(
            await self._json(
                "/intro/chkUser.do",
                {
                    "USER_ID": encrypted_id,
                    "USER_PWD": encrypted_password,
                    "USER_CI": "",
                    "TYPE": "I",
                },
            )
        )
        if result.get("result") != "success" or result.get("USER_SSO_YN") not in {"Y", "N"}:
            raise KepcoOnAuthError("Power Planner account check failed")
        _, target = await self._request(
            "POST",
            "/login",
            form={
                "USER_ID": encrypted_id,
                "USER_PWD": encrypted_password,
                "APT_YN": "Y"
                if len(username.strip()) > 10 or username.strip() == "user38"
                else "N",
                "SSO_ID": cast("str", result["USER_SSO_YN"]),
            },
        )
        if target is None or not await self._validate():
            raise KepcoOnAuthError("Power Planner login failed")
        await self._persist()
        self._authenticated = True

    async def _validate(self) -> bool:
        try:
            body, _ = await self._request("GET", LANDING)
        except KepcoOnSessionExpired:
            return False
        return _authenticated(body)

    async def _ensure(self, username: str, password: str | None) -> None:
        if self._authenticated:
            return
        if self._store is not None:
            try:
                session_id = await self._store.async_load()
            except KepcoOnProtocolError:
                await self._store.async_clear()
                session_id = None
            if session_id is not None:
                self.session.cookie_jar.update_cookies(
                    {"JSESSIONID": session_id}, response_url=ORIGIN
                )
                if await self._validate():
                    self._stored_id = session_id
                    self._authenticated = True
                    return
                await self._store.async_clear()
        if password is None:
            raise KepcoOnAuthError("Power Planner session requires authentication")
        await self.async_login(username, password)

    async def async_validate_customers(self, customers: set[str]) -> None:
        """Prove the queried main contract belongs to the selected household."""
        listed = _rows(await self._json("/auth/usercustno_list.do", {}))
        contract = _object(await self._json("/rm/rm0101_contract_info.do", {}))
        number = contract.get("CUSTNO")
        if (
            not isinstance(number, str)
            or not number
            or not any(row.get("CUSTNO") == number for row in listed)
        ):
            raise KepcoOnProtocolError("Power Planner contract identity is ambiguous")
        if number not in customers:
            raise KepcoOnNoCustomersError("Power Planner main customer is not selected")
        if contract.get("SESSION_MR_CYCLE_MIN") != 60:
            raise KepcoOnUnsupportedAccount("Power Planner meter profile is not supported")
        identity_fields = ("CUSTNO", "METER_ID", "CNTR_KND_NM", "SELBILL_CD", "MR_DD")
        if self._contract and any(
            self._contract.get(key) != contract.get(key) for key in identity_fields
        ):
            self._metrics.clear()
            self._months.clear()
            self._yesterday = None
            self._yesterday_fetched_at = None
        self._contract = contract
        self.main_customer_number = number

    async def async_poll(
        self,
        username: str,
        password: str | None,
        customers: set[str],
        *,
        fetch: bool = True,
        sources: frozenset[str] = frozenset(),
    ) -> PowerPlannerSnapshot | None:
        """Serialize data and keepalive requests; reauthenticate at most once."""
        async with self._lock:
            for attempt in range(2):
                try:
                    await self._ensure(username, password)
                    await self._json("/auth/refreshToken.do", {})
                    await self._persist()
                    if fetch or self._snapshot is None:
                        await self.async_validate_customers(customers)
                        self._snapshot = await self._read_snapshot()
                    if self._snapshot is not None:
                        self._snapshot = await self._extend_snapshot(self._snapshot, sources)
                    return self._snapshot
                except KepcoOnSessionExpired:
                    self._authenticated = False
                    self._stored_id = None
                    self.session.cookie_jar.clear()
                    self._snapshot = None
                    self._metrics.clear()
                    self._months.clear()
                    self._yesterday = None
                    self._yesterday_fetched_at = None
                    if self._store is not None:
                        await self._store.async_clear()
                    if attempt or password is None:
                        raise KepcoOnAuthError("Power Planner session expired") from None
            raise KepcoOnAuthError("Power Planner authentication failed")

    async def _read_snapshot(self) -> PowerPlannerSnapshot:
        today = self._clock().astimezone(KOREA).date()
        smart = _object(await self._json("/rm/getRM0201.do", {"menuType": "time", "TOU": False}))
        start, selected, end = (
            planner_date(smart.get(key)) for key in ("START_DT", "SELECT_DT", "END_DT")
        )
        if not start <= selected <= end or not start <= today <= end:
            raise KepcoOnProtocolError("Power Planner period is not current")
        values: dict[str, float | None] = {}
        statuses: dict[str, str] = {}
        for field_name, source in {
            "current_usage_kwh": "F_AP_QT",
            "predicted_usage_kwh": "PREDICT_TOT",
            "current_amount_krw": "TOTAL_CHARGE",
            "predicted_amount_krw": "PREDICT_TOTAL_CHARGE",
        }.items():
            try:
                values[field_name] = planner_quantity(smart.get(source))
                statuses[field_name] = "ok" if values[field_name] is not None else "no_data"
            except KepcoOnProtocolError:
                values[field_name], statuses[field_name] = None, "invalid_response"

        async def daily(day: date, completed: bool) -> DailyUsage:
            payload: dict[str, object] = {
                "SELECT_DT": day.isoformat(),
                "selectType": "all",
                "TIME_TYPE": "1",
            }
            totals = await self._json("/rs/rs0201_list.do", payload)
            chart = await self._json("/rs/rs0201_chart.do", payload)
            return parse_daily_metrics(totals, chart, completed=completed)

        try:
            daily_result = await daily(today, False)
            current, recent, valid, status = (
                daily_result.quantity,
                daily_result.recent,
                daily_result.count,
                daily_result.status,
            )
            interval_values = [quantity for _, quantity in daily_result.intervals]
            maximum = max(interval_values) if interval_values else None
            minimum = min(interval_values) if interval_values else None
            average = sum_quantities(interval_values) / valid if interval_values else None
            coverage = valid / 24 * 100
            statistic_status = "ok" if valid == 24 else "partial" if valid else "no_data"
        except KepcoOnProtocolError:
            current, recent, valid, status = None, None, 0, "invalid_response"
            maximum, minimum, average, coverage = None, None, None, None
            statistic_status = "invalid_response"
        yesterday = today - timedelta(days=1)
        if self._yesterday is None or self._yesterday[0] != yesterday or self._yesterday[3] != "ok":
            try:
                prior_day = await daily(yesterday, True)
                self._yesterday_fetched_at = self._clock().astimezone(KOREA)
                previous, count, previous_status = (
                    prior_day.quantity,
                    prior_day.count,
                    prior_day.status,
                )
            except KepcoOnProtocolError:
                previous, count, previous_status = None, 0, "invalid_response"
                self._yesterday_fetched_at = None
            self._yesterday = yesterday, previous, count, previous_status
        _, previous, count, previous_status = self._yesterday
        statuses.update(
            today_usage_kwh=status,
            yesterday_usage_kwh=previous_status,
            recent_interval_usage_kwh="ok" if recent is not None else status,
        )
        for field in (
            "today_max_interval_usage_kwh",
            "today_min_interval_usage_kwh",
            "today_average_interval_usage_kwh",
            "data_coverage_ratio_pct",
        ):
            statuses[field] = statistic_status
        predicted_level: int | None = None
        try:
            if "주택" not in str(self._contract.get("CNTR_KND_NM", "")):
                raise PlannerMetricError("unsupported_profile")
            predicted_level = planner_level(smart.get("PREDICT_BILL_LEVEL"))
            statuses["predicted_bill_level"] = "ok" if predicted_level is not None else "no_data"
        except PlannerMetricError as error:
            statuses["predicted_bill_level"] = error.status
        except KepcoOnProtocolError:
            statuses["predicted_bill_level"] = "invalid_response"
        fetched = self._clock().astimezone(KOREA)
        snapshot = PowerPlannerSnapshot(
            status="ok",
            period_start=start,
            period_end=end,
            reference_date=selected,
            calendar_date=today,
            interval_minutes=60,
            today_usage_kwh=current,
            today_max_interval_usage_kwh=maximum,
            today_min_interval_usage_kwh=minimum,
            today_average_interval_usage_kwh=average,
            data_coverage_ratio_pct=coverage,
            predicted_bill_level=predicted_level,
            source_metadata={
                "period": PowerPlannerSourceMetadata(
                    "meter_period", start, end, selected, fetched, status="ok"
                ),
                "day": PowerPlannerSourceMetadata(
                    "calendar_day", today, today, today, fetched, status=status
                ),
                "yesterday": PowerPlannerSourceMetadata(
                    "calendar_day",
                    yesterday,
                    yesterday,
                    yesterday,
                    self._yesterday_fetched_at,
                    status=previous_status,
                ),
            },
            yesterday_usage_kwh=previous,
            recent_interval_usage_kwh=recent,
            today_valid_intervals=valid,
            yesterday_valid_intervals=count,
            field_statuses=statuses,
        )
        return replace(
            snapshot,
            current_usage_kwh=values["current_usage_kwh"],
            predicted_usage_kwh=values["predicted_usage_kwh"],
            current_amount_krw=values["current_amount_krw"],
            predicted_amount_krw=values["predicted_amount_krw"],
        )

    async def _calendar_rows(self, year: int, month: int, now: datetime) -> tuple[CalendarDay, ...]:
        key = year, month
        cached = self._months.get(key)
        ttl = timedelta(hours=1 if key == (now.year, now.month) else 6)
        if (
            cached is not None
            and now - cached[0] < ttl
            and (key != (now.year, now.month) or cached[0].date() == now.date())
            and not ((cached[0].year, cached[0].month) == key and key != (now.year, now.month))
        ):
            return cached[1]
        payload: dict[str, object] = {
            "SELECT_MONTH": f"{year:04d}{month:02d}",
            "NEEDLE_TYPE": "MONTH",
            "selectType": "all",
        }
        rows = parse_calendar_month(await self._json("/rs/rs0202_chart.do", payload), year, month)
        self._months[key] = now, rows
        return rows

    async def _read_metric(
        self, source: str, snapshot: PowerPlannerSnapshot, now: datetime
    ) -> MetricResult:
        today = now.date()
        values: dict[str, float | int | None]
        statuses: dict[str, str] = {}
        if source == "tier":
            if "주택" not in str(self._contract.get("CNTR_KND_NM", "")):
                raise PlannerMetricError("unsupported_profile")
            detail = _object(await self._json("/re/re0202_list.do", {"SEL_METER_ID": ""}))
            if detail.get("CUSTNO") != self.main_customer_number:
                raise PlannerMetricError("unsupported_customer")
            if (planner_date(detail.get("START_DT")), planner_date(detail.get("END_DT"))) != (
                snapshot.period_start,
                snapshot.period_end,
            ):
                raise PlannerMetricError("period_mismatch")
            level = planner_level(detail.get("BILL_LEVEL"))
            values = {"current_bill_level": level}
            meta = PowerPlannerSourceMetadata(
                "meter_period",
                snapshot.period_start,
                snapshot.period_end,
                snapshot.reference_date,
                now,
                status="ok" if level is not None else "no_data",
            )
        elif source == "month":
            payload: dict[str, object] = {
                "SELECT_MONTH": f"{today:%Y%m}",
                "NEEDLE_TYPE": "MONTH",
                "selectType": "all",
            }
            total = await self._json("/rs/rs0202_list.do", payload)
            rows = await self._calendar_rows(today.year, today.month, now)
            quantity = calendar_total(rows, today)
            try:
                check_calendar_total(total, quantity, len(rows))
            except KepcoOnProtocolError:
                self._months.pop((today.year, today.month), None)
                raise
            values = {"calendar_month_usage_kwh": quantity}
            meta = PowerPlannerSourceMetadata(
                "calendar_month",
                today.replace(day=1),
                date(today.year, today.month, monthrange(today.year, today.month)[1]),
                today,
                now,
                status="partial",
            )
        elif source == "year":
            monthly_values = []
            for month in range(1, today.month + 1):
                monthly_values.append(
                    calendar_total(await self._calendar_rows(today.year, month, now), today)
                )
            values = {"calendar_year_usage_kwh": sum_quantities(monthly_values)}
            meta = PowerPlannerSourceMetadata(
                "calendar_year",
                date(today.year, 1, 1),
                date(today.year, 12, 31),
                today,
                now,
                status="partial",
            )
        elif source == "day_comparison":
            day = today - timedelta(days=1)
            try:
                baseline = day.replace(year=day.year - 1)
            except ValueError:
                raise PlannerMetricError("no_data") from None
            if self._yesterday is None or self._yesterday[0] != day or self._yesterday[3] != "ok":
                raise PlannerMetricError("incomplete")
            current = self._yesterday[1]
            rows = await self._calendar_rows(day.year, day.month, now)
            row = next(row for row in rows if row.day == day)
            if row.prior_year_status != "ok":
                raise PlannerMetricError(row.prior_year_status)
            if current is None or row.usage is None or row.prior_year_usage is None:
                raise PlannerMetricError("no_data")
            if not math.isclose(current, row.usage, rel_tol=0, abs_tol=0.02):
                raise PlannerMetricError("period_mismatch")
            difference = current - row.prior_year_usage
            values = {
                "prior_year_same_day_usage_kwh": row.prior_year_usage,
                "same_day_year_on_year_usage_difference_kwh": difference,
                "same_day_year_on_year_usage_ratio_pct": difference / row.prior_year_usage * 100
                if row.prior_year_usage
                else None,
            }
            if row.prior_year_usage == 0:
                statuses["same_day_year_on_year_usage_ratio_pct"] = "baseline_zero"
            elif not math.isfinite(cast("float", values["same_day_year_on_year_usage_ratio_pct"])):
                values["same_day_year_on_year_usage_ratio_pct"] = None
                statuses["same_day_year_on_year_usage_ratio_pct"] = "invalid_response"
            meta = PowerPlannerSourceMetadata(
                "calendar_day_comparison", day, day, day, now, comparison_date=baseline, status="ok"
            )
        else:
            body, _ = await self._request("GET", "/cc/cc0202.do")
            if b"RSA_USER_ID" in body:
                raise KepcoOnSessionExpired("Power Planner session expired")
            text = body.decode("utf-8", errors="replace")
            if "내순위(상위%)" not in text:
                raise PlannerMetricError("comparison_scope_unverified")
            defaults = ComparisonDefaults()
            defaults.feed(text)
            form = defaults.validated(today)
            comparison_values, statuses = parse_neighbors(
                await self._form_json("/cc/cc0202Info.do", form)
            )
            values = dict(comparison_values)
            period = f"{int(form['year']):04d}{int(form['month']):02d}"
            meta = PowerPlannerSourceMetadata(
                "provider_comparison_month",
                reference_date=today,
                fetched_at=now,
                comparison_period=period,
                comparison_scope="portal_default_region",
                status="ok",
            )
        for field, value in values.items():
            statuses.setdefault(field, meta.status if value is not None else "no_data")
        return MetricResult(values, meta, statuses)

    async def _extend_snapshot(
        self, snapshot: PowerPlannerSnapshot, sources: frozenset[str]
    ) -> PowerPlannerSnapshot:
        now = self._clock().astimezone(KOREA)
        self._months = {
            key: value
            for key, value in self._months.items()
            if (key[0] == now.year and key[1] <= now.month) or key == (now.year - 1, 12)
        }
        updates: dict[str, float | int | None] = {}
        statuses = dict(snapshot.field_statuses)
        metadata = dict(snapshot.source_metadata)
        for source, fields in METRIC_FIELDS.items():
            if source not in sources:
                updates.update(dict.fromkeys(fields))
                statuses.update(dict.fromkeys(fields, "not_requested"))
                metadata.pop(source, None)
                continue
            key: tuple[object, ...] = (
                (now.year,)
                if source == "year"
                else (snapshot.period_start, snapshot.period_end)
                if source == "tier"
                else (now.date(),)
            )
            ttl = timedelta(hours=6 if source == "year" else 24 if source == "neighbors" else 1)
            cached = self._metrics.get(source)
            if cached is not None and cached.key == key:
                if any(
                    status
                    in {
                        "connection_error",
                        "invalid_response",
                        "rate_limited",
                    }
                    for status in cached.result.statuses.values()
                ):
                    ttl = timedelta(minutes=20)
                if now - cached.attempted_at < ttl:
                    updates.update(cached.result.values)
                    statuses.update(cached.result.statuses)
                    metadata[source] = cached.result.metadata
                    continue
            try:
                if (
                    source == "year"
                    and "month" in sources
                    and metadata.get("month") is not None
                    and metadata["month"].status not in {"ok", "partial"}
                ):
                    raise PlannerMetricError(metadata["month"].status)
                result = await self._read_metric(source, snapshot, now)
            except PlannerMetricError as error:
                status = error.status
            except KepcoOnRateLimitError:
                status = "rate_limited"
            except KepcoOnConnectionError:
                status = "connection_error"
            except KepcoOnProtocolError:
                status = "invalid_response"
            else:
                self._metrics[source] = MetricCache(key, now, result)
                updates.update(result.values)
                statuses.update(result.statuses)
                metadata[source] = result.metadata
                continue
            last_success = (
                cached.result.metadata.fetched_at
                if cached is not None and cached.key == key
                else None
            )
            result = MetricResult(
                dict.fromkeys(fields),
                PowerPlannerSourceMetadata(
                    source, reference_date=now.date(), fetched_at=last_success, status=status
                ),
                dict.fromkeys(fields, status),
            )
            self._metrics[source] = MetricCache(key, now, result)
            updates.update(result.values)
            statuses.update(result.statuses)
            metadata[source] = result.metadata
        return replace(
            snapshot,
            current_bill_level=cast("int | None", updates["current_bill_level"]),
            calendar_month_usage_kwh=updates["calendar_month_usage_kwh"],
            calendar_year_usage_kwh=updates["calendar_year_usage_kwh"],
            prior_year_same_day_usage_kwh=updates["prior_year_same_day_usage_kwh"],
            same_day_year_on_year_usage_difference_kwh=updates[
                "same_day_year_on_year_usage_difference_kwh"
            ],
            same_day_year_on_year_usage_ratio_pct=updates["same_day_year_on_year_usage_ratio_pct"],
            regional_average_usage_kwh=updates["regional_average_usage_kwh"],
            saving_neighbor_usage_kwh=updates["saving_neighbor_usage_kwh"],
            regional_usage_percentile_pct=updates["regional_usage_percentile_pct"],
            regional_average_amount_krw=updates["regional_average_amount_krw"],
            saving_neighbor_amount_krw=updates["saving_neighbor_amount_krw"],
            regional_amount_percentile_pct=updates["regional_amount_percentile_pct"],
            field_statuses=statuses,
            source_metadata=metadata,
        )
