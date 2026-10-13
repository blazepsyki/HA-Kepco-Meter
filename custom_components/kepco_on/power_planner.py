"""Verified Power Planner authentication, session storage, and read contracts."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
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
from .models import PowerPlannerSnapshot

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
    }
)
NUMBER = re.compile(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?")
# RFC 6265 cookie-octet: opaque session cookies need not be alphanumeric.
SESSION_ID = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,1024}")
KOREA = ZoneInfo("Asia/Seoul")


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


def _authenticated(body: bytes) -> bool:
    text = body.decode("utf-8", errors="replace")
    return (
        re.search(r"id=[\"']RSA_USER_(?:ID|PWD)[\"']", text) is None
        and re.search(r"logout|로그아웃", text, re.IGNORECASE) is not None
        and re.search(r"스마트뷰|실시간사용량|전기사용량", text) is not None
    )


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


def parse_daily(
    total: object, chart: object, *, completed: bool
) -> tuple[float | None, float | None, int, str]:
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
        return None, None, 0, "no_data"
    recent = max(valid)[1]
    if completed and len(valid) != 24:
        return None, recent, len(valid), "incomplete"
    quantity = planner_quantity(totals[0].get("F_AP_QT"))
    status = "no_data" if quantity is None else "ok" if len(valid) == 24 else "partial"
    return quantity, recent, len(valid), status


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

    async def _request(
        self, method: str, path: str, *, payload: object = None, form: dict[str, str] | None = None
    ) -> tuple[bytes, str | None]:
        if path not in PATHS or method not in {"GET", "POST"}:
            raise KepcoOnProtocolError("Power Planner request path is not allowed")
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
        self.main_customer_number = number

    async def async_poll(
        self, username: str, password: str | None, customers: set[str], *, fetch: bool = True
    ) -> PowerPlannerSnapshot | None:
        """Serialize data and keepalive requests; reauthenticate at most once."""
        async with self._lock:
            for attempt in range(2):
                try:
                    await self._ensure(username, password)
                    await self._json("/auth/refreshToken.do", {})
                    await self._persist()
                    if fetch:
                        await self.async_validate_customers(customers)
                        self._snapshot = await self._read_snapshot()
                    return self._snapshot
                except KepcoOnSessionExpired:
                    self._authenticated = False
                    self._stored_id = None
                    self.session.cookie_jar.clear()
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

        async def daily(day: date, completed: bool) -> tuple[float | None, float | None, int, str]:
            payload: dict[str, object] = {
                "SELECT_DT": day.isoformat(),
                "selectType": "all",
                "TIME_TYPE": "1",
            }
            totals = await self._json("/rs/rs0201_list.do", payload)
            chart = await self._json("/rs/rs0201_chart.do", payload)
            return parse_daily(totals, chart, completed=completed)

        try:
            current, recent, valid, status = await daily(today, False)
        except KepcoOnProtocolError:
            current, recent, valid, status = None, None, 0, "invalid_response"
        yesterday = today - timedelta(days=1)
        if self._yesterday is None or self._yesterday[0] != yesterday or self._yesterday[3] != "ok":
            try:
                previous, _, count, previous_status = await daily(yesterday, True)
            except KepcoOnProtocolError:
                previous, count, previous_status = None, 0, "invalid_response"
            self._yesterday = yesterday, previous, count, previous_status
        _, previous, count, previous_status = self._yesterday
        statuses.update(
            today_usage_kwh=status,
            yesterday_usage_kwh=previous_status,
            recent_interval_usage_kwh="ok" if recent is not None else status,
        )
        snapshot = PowerPlannerSnapshot(
            status="ok",
            period_start=start,
            period_end=end,
            reference_date=selected,
            calendar_date=today,
            interval_minutes=60,
            today_usage_kwh=current,
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
