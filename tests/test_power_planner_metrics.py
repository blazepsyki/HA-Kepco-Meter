"""Synthetic boundary and source-isolation regressions for planner A-C metrics."""

from __future__ import annotations

from calendar import monthrange
from collections.abc import AsyncGenerator
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiohttp import ClientSession, CookieJar
from custom_components.kepco_on.const import CONF_USERNAME
from custom_components.kepco_on.exceptions import (
    KepcoOnConnectionError,
    KepcoOnProtocolError,
    KepcoOnRateLimitError,
    KepcoOnSessionExpired,
)
from custom_components.kepco_on.models import PowerPlannerData, PowerPlannerSnapshot
from custom_components.kepco_on.power_planner import ORIGIN, PowerPlannerClient
from custom_components.kepco_on.power_planner_coordinator import PowerPlannerCoordinator
from custom_components.kepco_on.power_planner_parser import (
    ComparisonDefaults,
    PlannerMetricError,
    calendar_total,
    check_calendar_total,
    parse_calendar_month,
    parse_neighbors,
    planner_level,
    sum_quantities,
)
from custom_components.kepco_on.sensor import PLANNER_DESCRIPTIONS, PowerPlannerSensor
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler

from .test_api_house import _house_customer
from .test_power_planner_site import NOW, smart
from .test_sensor import FakeDeviceRegistry, FakeEntityRegistry, entity_entry

DEFAULT_DAY = NOW.date()


def calendar_rows(year: int, month: int, today: date = DEFAULT_DAY) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for day in range(1, monthrange(year, month)[1] + 1):
        target = date(year, month, day)
        usage = 0.0 if target > today else 1.0
        if target == today:
            usage = 16.0
        if target == today - timedelta(days=1):
            usage = 24.0
        rows.append(
            {"MR_YMD2": f"{month:02d}.{day:02d}", "F_AP_QT": str(usage), "LYEAR_F_AP_QT": "12"}
        )
    return rows


def portal_defaults(year: str = "2026", month: str = "08") -> str:
    values = {
        "year": year,
        "month": month,
        "SIDO_CD": "TEST_REGION",
        "SIGUNGU_CD": "",
        "DONGCD": "",
    }
    return "내순위(상위%)" + "".join(
        f'<select id="{key}"><option value="{value}" selected>Default</option></select>'
        for key, value in values.items()
    )


def neighbors() -> list[dict[str, object]]:
    return [
        {
            "AA": "사용량",
            "F_AP_QT_ALL": "9999",
            "F_AP_QT_ALL_DISP": 100,
            "F_AP_QT_SAVE_DISP": 50,
            "RANK_RATE": 25,
        },
        {
            "AA": "요금",
            "F_AP_QT_ALL": "8888",
            "F_AP_QT_ALL_DISP": 20000,
            "F_AP_QT_SAVE_DISP": 10000,
            "RANK_RATE": 75,
        },
    ]


class MetricsClient(PowerPlannerClient):
    def __init__(self, session: ClientSession) -> None:
        self.now = NOW
        super().__init__(session, clock=lambda: self.now)
        self.main_customer_number = "TEST_SI_CUST_001"
        self._contract = {
            "CUSTNO": "TEST_SI_CUST_001",
            "SESSION_MR_CYCLE_MIN": 60,
            "CNTR_KND_NM": "주택용(저압)",
            "METER_ID": "TEST_METER",
        }
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.failures: dict[str, Exception] = {}
        self.current = smart()
        self.current["PREDICT_BILL_LEVEL"] = "3"
        self.detail: dict[str, object] = {
            "CUSTNO": "TEST_SI_CUST_001",
            "START_DT": "2026.08.09",
            "END_DT": "2026.09.08",
            "BILL_LEVEL": "2",
        }
        self.portal = portal_defaults()
        self.comparison = neighbors()
        self.baseline = "12"
        self.yesterday_valid = 24

    async def _json(self, path: str, payload: dict[str, object]) -> object:
        self.calls.append((path, payload))
        if path in self.failures:
            raise self.failures[path]
        if path == "/auth/refreshToken.do":
            return {}
        if path == "/auth/usercustno_list.do":
            return [{"CUSTNO": self.main_customer_number}]
        if path == "/rm/rm0101_contract_info.do":
            return self._contract
        if path == "/rm/getRM0201.do":
            return self.current
        if path == "/re/re0202_list.do":
            assert payload == {"SEL_METER_ID": ""}
            return self.detail
        if path.startswith("/rs/rs0201"):
            today = payload["SELECT_DT"] == self.now.date().isoformat()
            if path.endswith("_list.do"):
                return [{"F_AP_QT": "16" if today else "24"}]
            return [
                {
                    "MR_HHMI": f"{hour:02d}시",
                    "F_AP_QT": "1",
                    "NO_DATA_YN": "N" if hour < (16 if today else self.yesterday_valid) else "Y",
                }
                for hour in range(24)
            ]
        if path.startswith("/rs/rs0202"):
            assert payload["NEEDLE_TYPE"] == "MONTH"
            period = str(payload["SELECT_MONTH"])
            assert len(period) == 6
            rows = calendar_rows(int(period[:4]), int(period[4:]), self.now.date())
            for row in rows:
                row["LYEAR_F_AP_QT"] = self.baseline
            if path.endswith("_list.do"):
                return [{"F_AP_QT": str(sum(float(cast("str", row["F_AP_QT"])) for row in rows))}]
            return rows
        raise AssertionError(path)

    async def _request(
        self, method: str, path: str, *, payload: object = None, form: dict[str, str] | None = None
    ) -> tuple[bytes, str | None]:
        assert method == "GET"
        assert path == "/cc/cc0202.do"
        self.calls.append((path, {}))
        if path in self.failures:
            raise self.failures[path]
        return self.portal.encode(), None

    async def _form_json(self, path: str, payload: dict[str, str]) -> object:
        self.calls.append((path, cast("dict[str, object]", payload)))
        assert path == "/cc/cc0202Info.do"
        assert set(payload) == {"year", "month", "SIDO_CD", "SIGUNGU_CD", "DONGCD"}
        return self.comparison

    async def snapshot(self, sources: frozenset[str] = frozenset()) -> PowerPlannerSnapshot:
        return await self._extend_snapshot(await self._read_snapshot(), sources)


@pytest.fixture
async def client() -> AsyncGenerator[MetricsClient]:
    async with ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session:
        yield MetricsClient(session)


async def test_all_metrics_have_distinct_scopes_and_calendar_year_uses_month_mode(
    client: MetricsClient,
) -> None:
    result = await client.snapshot(
        frozenset({"tier", "month", "year", "day_comparison", "neighbors"})
    )
    assert result.today_max_interval_usage_kwh == 1
    assert result.today_min_interval_usage_kwh == 1
    assert result.today_average_interval_usage_kwh == 1
    assert result.data_coverage_ratio_pct == pytest.approx(16 / 24 * 100)
    assert result.current_bill_level == 2
    assert result.predicted_bill_level == 3
    assert result.calendar_month_usage_kwh == 54
    assert result.calendar_year_usage_kwh == 266
    assert result.prior_year_same_day_usage_kwh == 12
    assert result.same_day_year_on_year_usage_difference_kwh == 12
    assert result.same_day_year_on_year_usage_ratio_pct == 100
    assert result.regional_average_usage_kwh == 100
    assert result.regional_average_amount_krw == 20000
    assert result.regional_usage_percentile_pct == 25
    assert result.regional_amount_percentile_pct == 75
    assert result.source_metadata["month"].period_start == date(2026, 8, 1)
    assert result.source_metadata["year"].period_start == date(2026, 1, 1)
    assert result.source_metadata["day_comparison"].reference_date == date(2026, 8, 15)
    assert result.source_metadata["day_comparison"].comparison_date == date(2025, 8, 15)
    assert result.source_metadata["neighbors"].comparison_scope == "portal_default_region"
    charts = [
        payload["SELECT_MONTH"] for path, payload in client.calls if path == "/rs/rs0202_chart.do"
    ]
    assert charts == [
        "202608",
        "202601",
        "202602",
        "202603",
        "202604",
        "202605",
        "202606",
        "202607",
    ]
    assert all("rs0203" not in path for path, _ in client.calls)


async def test_disabled_optional_sources_make_no_new_requests(client: MetricsClient) -> None:
    result = await client.snapshot()
    assert all(not path.startswith(("/cc/", "/re/", "/rs/rs0202")) for path, _ in client.calls)
    assert result.calendar_year_usage_kwh is None
    assert result.field_statuses["calendar_year_usage_kwh"] == "not_requested"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (KepcoOnConnectionError("PRIVATE"), "connection_error"),
        (KepcoOnRateLimitError("PRIVATE"), "rate_limited"),
        (KepcoOnProtocolError("PRIVATE"), "invalid_response"),
    ],
)
async def test_calendar_failure_keeps_basic_values_and_other_sources(
    client: MetricsClient, error: Exception, expected: str
) -> None:
    client.failures["/rs/rs0202_list.do"] = error
    result = await client.snapshot(frozenset({"tier", "month", "year", "neighbors"}))
    assert result.current_usage_kwh == 123.45
    assert result.today_usage_kwh == 16
    assert result.current_bill_level == 2
    assert result.regional_average_usage_kwh == 100
    assert result.calendar_month_usage_kwh is None
    assert result.calendar_year_usage_kwh is None
    assert result.field_statuses["calendar_month_usage_kwh"] == expected
    assert result.field_statuses["calendar_year_usage_kwh"] == expected
    assert "PRIVATE" not in str(result)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("customer", "unsupported_customer"),
        ("period", "period_mismatch"),
        ("profile", "unsupported_profile"),
        ("level", "unsupported_profile"),
        ("date", "invalid_response"),
        ("missing", "no_data"),
    ],
)
async def test_tier_requires_selected_household_period_and_valid_level(
    client: MetricsClient, case: str, expected: str
) -> None:
    if case == "customer":
        client.detail["CUSTNO"] = "OTHER_PRIVATE_CUSTOMER"
    elif case == "period":
        client.detail["START_DT"] = "2026.07.09"
    elif case == "profile":
        client._contract["CNTR_KND_NM"] = "일반용"
    elif case == "level":
        client.detail["BILL_LEVEL"] = "4"
    elif case == "date":
        client.detail["START_DT"] = "bad"
    else:
        client.detail["BILL_LEVEL"] = None
    result = await client.snapshot(frozenset({"tier"}))
    assert result.current_bill_level is None
    assert result.field_statuses["current_bill_level"] == expected
    assert result.today_usage_kwh == 16
    assert "OTHER_PRIVATE_CUSTOMER" not in str(result)


async def test_source_caches_and_disabling_clear_optional_values(client: MetricsClient) -> None:
    sources = frozenset({"tier", "month", "year", "neighbors"})
    first = await client.snapshot(sources)
    before = len(client.calls)
    second = await client._extend_snapshot(first, sources)
    assert second == first
    assert len(client.calls) == before
    client.now += timedelta(hours=2)
    third = await client.snapshot(sources)
    assert third.source_metadata["year"].fetched_at == first.source_metadata["year"].fetched_at
    assert third.source_metadata["month"].fetched_at != first.source_metadata["month"].fetched_at
    disabled = await client._extend_snapshot(third, frozenset())
    assert disabled.regional_average_usage_kwh is None
    assert disabled.current_bill_level is None
    assert "neighbors" not in disabled.source_metadata


async def test_source_failure_does_not_republish_cached_success(client: MetricsClient) -> None:
    first = await client.snapshot(frozenset({"tier"}))
    client.now += timedelta(hours=1)
    client.failures["/re/re0202_list.do"] = KepcoOnConnectionError("PRIVATE")
    failed = await client.snapshot(frozenset({"tier"}))
    assert failed.current_bill_level is None
    assert failed.source_metadata["tier"].fetched_at == first.source_metadata["tier"].fetched_at
    before = len(client.calls)
    client.now += timedelta(minutes=10)
    await client._extend_snapshot(failed, frozenset({"tier"}))
    assert len(client.calls) == before


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("zero", "baseline_zero"),
        ("missing", "no_data"),
        ("invalid", "invalid_response"),
        ("partial", "incomplete"),
        ("period", "period_mismatch"),
    ],
)
async def test_day_comparison_never_compares_partial_today_to_complete_prior_day(
    client: MetricsClient, case: str, expected: str
) -> None:
    if case == "zero":
        client.baseline = "0"
    elif case == "missing":
        client.baseline = cast("Any", None)
    elif case == "invalid":
        client.baseline = "NaN"
    elif case == "partial":
        client.yesterday_valid = 23
    else:
        base = await client._read_snapshot()
        client._yesterday = NOW.date() - timedelta(days=1), 25, 24, "ok"
        result = await client._extend_snapshot(base, frozenset({"day_comparison"}))
        assert result.field_statuses["same_day_year_on_year_usage_ratio_pct"] == expected
        return
    result = await client.snapshot(frozenset({"day_comparison"}))
    assert result.same_day_year_on_year_usage_ratio_pct is None
    assert result.field_statuses["same_day_year_on_year_usage_ratio_pct"] == expected
    if case == "zero":
        assert result.same_day_year_on_year_usage_difference_kwh == 24
        assert result.prior_year_same_day_usage_kwh == 0


def test_calendar_binding_excludes_future_zeros_without_excluding_real_zero() -> None:
    rows = calendar_rows(2026, 8)
    rows[0]["F_AP_QT"] = "0"
    parsed = parse_calendar_month(rows, 2026, 8)
    assert calendar_total(parsed, NOW.date()) == 53
    check_calendar_total([{"F_AP_QT": "53"}], 53, 31)


@pytest.mark.parametrize(
    "case",
    [
        "short",
        "wrong_month",
        "label",
        "invalid_date",
        "duplicate",
        "marker",
        "quantity",
        "future_positive",
        "missing",
        "sum",
    ],
)
def test_calendar_invalid_ranges_and_totals_are_rejected(case: str) -> None:
    rows = calendar_rows(2026, 8)
    if case == "short":
        rows.pop()
    elif case == "wrong_month":
        rows[0]["MR_YMD2"] = "07.01"
    elif case == "label":
        rows[0]["MR_YMD2"] = None
    elif case == "invalid_date":
        rows[0]["MR_YMD2"] = "08.32"
    elif case == "duplicate":
        rows[1]["MR_YMD2"] = "08.01"
    elif case == "marker":
        rows[0]["NO_DATA_YN"] = "X"
    elif case == "quantity":
        rows[0]["F_AP_QT"] = "NaN"
    elif case == "future_positive":
        rows[-1]["F_AP_QT"] = "1"
    elif case == "missing":
        rows[0]["NO_DATA_YN"] = "Y"
    if case in {"future_positive", "missing"}:
        parsed = parse_calendar_month(rows, 2026, 8)
        with pytest.raises(KepcoOnProtocolError):
            calendar_total(parsed, NOW.date())
    elif case == "sum":
        with pytest.raises(KepcoOnProtocolError):
            check_calendar_total([{"F_AP_QT": "999"}], 54, 31)
    else:
        with pytest.raises(KepcoOnProtocolError):
            parse_calendar_month(rows, 2026, 8)


@pytest.mark.parametrize("total", [[], {}, [{"F_AP_QT": None}]])
def test_calendar_missing_aggregate_is_not_zero(total: object) -> None:
    with pytest.raises(KepcoOnProtocolError):
        check_calendar_total(total, 1, 31)


@pytest.mark.parametrize("level", [0, 4, 1.5, "unknown"])
def test_unknown_tier_is_never_guessed(level: object) -> None:
    with pytest.raises(KepcoOnProtocolError):
        planner_level(level)


@pytest.mark.parametrize("case", ["empty", "extra", "duplicate", "role", "missing_role"])
def test_comparison_role_and_percentile_contract_is_strict(case: str) -> None:
    rows = neighbors()
    if case == "empty":
        rows = []
    elif case == "extra":
        rows.append(dict(rows[0]))
    elif case == "duplicate":
        rows[1]["AA"] = "사용량"
    elif case == "role":
        rows[0]["AA"] = "unknown"
    else:
        rows[0]["AA"] = None
    with pytest.raises(KepcoOnProtocolError):
        parse_neighbors(rows)


@pytest.mark.parametrize(
    "html",
    [
        "",
        portal_defaults("bad"),
        portal_defaults(month="13"),
        portal_defaults("2027"),
        portal_defaults().replace("TEST_REGION", "bad space"),
    ],
)
def test_unverified_comparison_defaults_are_rejected(html: str) -> None:
    parser = ComparisonDefaults()
    parser.feed(html)
    with pytest.raises(PlannerMetricError):
        parser.validated(NOW.date())


async def test_registry_sources_default_off_and_user_enable_are_respected(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = FakeEntityRegistry([])
    monkeypatch.setattr(er, "async_get", lambda hass: registry)
    monkeypatch.setattr(er, "async_entries_for_config_entry", lambda reg, entry_id: reg.entries)
    entry = SimpleNamespace(
        entry_id="planner", data={CONF_USERNAME: "PRIVATE"}, async_on_unload=lambda callback: None
    )
    coordinator = PowerPlannerCoordinator(
        hass, cast("Any", entry), cast("Any", object()), (_house_customer(),), None
    )
    assert coordinator._enabled_sources() == frozenset({"tier", "month", "year"})
    registry.entries.append(
        entity_entry(
            "sensor.optional",
            "house-key_power_planner_regional_average_usage",
            config_entry_id="planner",
        )
    )
    registry.entries.append(
        entity_entry(
            "sensor.year",
            "house-key_power_planner_calendar_year_usage",
            config_entry_id="planner",
            disabled_by=RegistryEntryDisabler.USER,
        )
    )
    assert coordinator._enabled_sources() == frozenset({"tier", "month", "neighbors"})


async def test_optional_default_disabled_registry_entries_are_never_reenabled_on_reload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.kepco_on import sensor
    from custom_components.kepco_on.const import (
        OPT_ENABLE_POWER_PLANNER,
        OPT_POWER_PLANNER_CUSTOMER_ID,
    )
    from homeassistant.helpers import device_registry as dr

    registry = FakeEntityRegistry(
        [
            entity_entry(
                "sensor.optional",
                "house-key_power_planner_regional_average_usage",
                disabled_by=RegistryEntryDisabler.INTEGRATION,
            )
        ]
    )
    monkeypatch.setattr(er, "async_get", lambda hass: registry)
    monkeypatch.setattr(er, "async_entries_for_config_entry", lambda reg, entry_id: reg.entries)
    devices = FakeDeviceRegistry([])
    monkeypatch.setattr(dr, "async_get", lambda hass: devices)
    monkeypatch.setattr(dr, "async_entries_for_config_entry", lambda reg, entry_id: reg.entries)
    entry = SimpleNamespace(
        entry_id="entry-1",
        options={OPT_ENABLE_POWER_PLANNER: True, OPT_POWER_PLANNER_CUSTOMER_ID: "house-key"},
    )
    await sensor._async_remove_stale_registry_entries(
        hass, cast("Any", entry), (_house_customer(),)
    )
    assert registry.updated == []


async def test_sensor_units_and_periods_do_not_inherit_currency(client: MetricsClient) -> None:
    result = await client.snapshot(frozenset({"tier", "month", "day_comparison", "neighbors"}))
    coordinator = SimpleNamespace(
        data=PowerPlannerData({"house-key": result}), last_update_success=True
    )
    by_key = {
        d.key: PowerPlannerSensor(cast("Any", coordinator), _house_customer(), d)
        for d in PLANNER_DESCRIPTIONS
    }
    tier = by_key["power_planner_current_bill_level"]
    assert tier.native_unit_of_measurement is None
    assert tier.extra_state_attributes["conversion_basis"] == "provider_level"
    month = by_key["power_planner_calendar_month_usage"]
    assert month.extra_state_attributes["usage_period_start"] == date(2026, 8, 1)
    comparison = by_key["power_planner_regional_usage_percentile"]
    assert comparison.native_unit_of_measurement == "%"
    assert comparison.extra_state_attributes["comparison_period"] == "202608"
    assert comparison.extra_state_attributes["rank_basis"] == "provider_top_percent"
    assert "TEST_REGION" not in str(comparison.extra_state_attributes)
    assert "TEST_SI_CUST_001" not in str(comparison.extra_state_attributes)


def test_overflowing_quantity_sums_are_protocol_errors() -> None:
    with pytest.raises(KepcoOnProtocolError):
        sum_quantities([1e308, 1e308])


async def test_session_expiration_is_not_swallowed_as_an_optional_failure(
    client: MetricsClient,
) -> None:
    client.failures["/rs/rs0202_list.do"] = KepcoOnSessionExpired("PRIVATE")
    with pytest.raises(KepcoOnSessionExpired):
        await client.snapshot(frozenset({"month"}))


async def test_previous_day_metadata_keeps_its_own_date_and_cached_fetch_time(
    client: MetricsClient,
) -> None:
    result = await client.snapshot()
    assert result.source_metadata["yesterday"].reference_date == date(2026, 8, 15)
    fetched = result.source_metadata["yesterday"].fetched_at
    client.now += timedelta(hours=1)
    second = await client.snapshot()
    assert second.source_metadata["yesterday"].fetched_at == fetched
    description = next(d for d in PLANNER_DESCRIPTIONS if d.key == "power_planner_yesterday_usage")
    coordinator = SimpleNamespace(
        data=PowerPlannerData({"house-key": second}), last_update_success=True
    )
    entity = PowerPlannerSensor(cast("Any", coordinator), _house_customer(), description)
    assert entity.extra_state_attributes["usage_period_start"] == date(2026, 8, 15)
    assert entity.extra_state_attributes["reference_date"] == date(2026, 8, 15)
    assert entity.extra_state_attributes["calendar_date"] == date(2026, 8, 15)


async def test_closed_month_is_refetched_after_month_rollover(client: MetricsClient) -> None:
    client.now = client.now.replace(day=31)
    old = await client._calendar_rows(2026, 8, client.now)
    assert old[-1].usage == 16
    client.now = client.now.replace(month=9, day=1)
    new = await client._calendar_rows(2026, 8, client.now)
    assert new[-1].usage == 24
    assert len(client.calls) == 2


async def test_new_calendar_year_does_not_reuse_previous_year_total(client: MetricsClient) -> None:
    first = await client.snapshot(frozenset({"year"}))
    client.now = client.now.replace(year=2027, month=1, day=1)
    base = PowerPlannerSnapshot(status="ok", calendar_date=client.now.date())
    result = await client._extend_snapshot(base, frozenset({"year"}))
    assert result.source_metadata["year"].period_start == date(2027, 1, 1)
    assert result.calendar_year_usage_kwh == 16
    assert result.calendar_year_usage_kwh != first.calendar_year_usage_kwh
    assert all(key[0] == 2027 or key == (2026, 12) for key in client._months)


async def test_leap_day_comparison_has_no_invented_prior_year_date(client: MetricsClient) -> None:
    client.now = client.now.replace(year=2024, month=3, day=1)
    base = PowerPlannerSnapshot(status="ok", calendar_date=client.now.date())
    result = await client._extend_snapshot(base, frozenset({"day_comparison"}))
    assert result.prior_year_same_day_usage_kwh is None
    assert result.field_statuses["prior_year_same_day_usage_kwh"] == "no_data"
    assert client.calls == []


async def test_contract_identity_change_invalidates_all_metric_caches(
    client: MetricsClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await client.snapshot(frozenset({"month", "year"}))
    assert client._months
    client._contract = dict(client._contract)
    client._contract["METER_ID"] = "TEST_CHANGED_METER"
    # Simulate a new contract response without mutating the saved old object.
    old_contract = dict(client._contract)
    old_contract["METER_ID"] = "TEST_METER"
    actual_response = client._contract
    client._contract = old_contract
    original = client._json

    async def response(path: str, payload: dict[str, object]) -> object:
        if path == "/rm/rm0101_contract_info.do":
            return actual_response
        return await original(path, payload)

    monkeypatch.setattr(client, "_json", response)
    await client.async_validate_customers({"TEST_SI_CUST_001"})
    assert client._months == {}
    assert client._metrics == {}
    assert client._yesterday is None


@pytest.mark.parametrize("case", ["login", "rank_label", "selectors"])
async def test_comparison_page_requires_authentication_and_known_scope(
    client: MetricsClient, case: str
) -> None:
    if case == "login":
        client.portal = '<input id="RSA_USER_ID">'
        with pytest.raises(KepcoOnSessionExpired):
            await client.snapshot(frozenset({"neighbors"}))
        return
    if case == "rank_label":
        client.portal = client.portal.replace("내순위(상위%)", "")
    else:
        client.portal = "내순위(상위%)"
    result = await client.snapshot(frozenset({"neighbors"}))
    assert result.regional_average_usage_kwh is None
    assert result.field_statuses["regional_average_usage_kwh"] == "comparison_scope_unverified"
    assert result.current_usage_kwh == 123.45


async def test_calendar_aggregate_mismatch_discards_unverified_rows(
    client: MetricsClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = client._json

    async def response(path: str, payload: dict[str, object]) -> object:
        if path == "/rs/rs0202_list.do":
            return [{"F_AP_QT": "999"}]
        return await original(path, payload)

    monkeypatch.setattr(client, "_json", response)
    result = await client.snapshot(frozenset({"month", "year"}))
    assert result.calendar_month_usage_kwh is None
    assert result.calendar_year_usage_kwh is None
    assert client._months == {}


@pytest.mark.usefixtures("socket_enabled")
async def test_429_backoff_prevents_new_http_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp import ClientConnectionError
    from aresponses import Response, ResponsesMockServer

    async with (
        ResponsesMockServer() as server,
        ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session,
    ):
        server.add("pp.kepco.co.kr", "/rs/rs0202_list.do", "post", Response(status=429))
        client = PowerPlannerClient(session, clock=lambda: NOW)
        with pytest.raises(KepcoOnRateLimitError):
            await client._json("/rs/rs0202_list.do", {})

        def must_not_request(*args: object, **kwargs: object) -> Any:
            raise ClientConnectionError("must not request")

        monkeypatch.setattr(session, "request", must_not_request)
        with pytest.raises(KepcoOnRateLimitError):
            await client._json("/re/re0202_list.do", {})


def test_selected_option_overrides_first_without_losing_control_defaults() -> None:
    parser = ComparisonDefaults()
    parser.feed(
        portal_defaults().replace(
            '<option value="2026" selected>',
            '<option value="2025">Old</option><option value="2026" selected>',
        )
    )
    assert parser.validated(NOW.date())["year"] == "2026"
    parser.feed('<select id="unrelated"><option value="PRIVATE">Private</option></select>')
    assert "unrelated" not in parser.values


def test_nonfinite_sum_and_nullable_level_remain_safe() -> None:
    with pytest.raises(KepcoOnProtocolError):
        sum_quantities([float("inf")])
    assert planner_level(None) is None


async def test_finite_comparison_inputs_cannot_publish_infinite_ratio(
    client: MetricsClient,
) -> None:
    client.baseline = "0." + "0" * 307 + "1"
    result = await client.snapshot(frozenset({"day_comparison"}))
    assert result.prior_year_same_day_usage_kwh is not None
    assert result.same_day_year_on_year_usage_ratio_pct is None
    assert result.field_statuses["same_day_year_on_year_usage_ratio_pct"] == "invalid_response"


async def test_bad_baseline_does_not_erase_calendar_usage(client: MetricsClient) -> None:
    client.baseline = "NaN"
    result = await client.snapshot(frozenset({"month", "year", "day_comparison"}))
    assert result.calendar_month_usage_kwh == 54
    assert result.calendar_year_usage_kwh == 266
    assert result.prior_year_same_day_usage_kwh is None
    assert result.field_statuses["prior_year_same_day_usage_kwh"] == "invalid_response"


@pytest.mark.parametrize("value", [101, "NaN", None])
async def test_one_invalid_comparison_field_preserves_other_values(
    client: MetricsClient,
    value: object,
) -> None:
    client.comparison[0]["RANK_RATE"] = cast("Any", value)
    result = await client.snapshot(frozenset({"neighbors"}))
    assert result.regional_usage_percentile_pct is None
    assert result.regional_average_usage_kwh == 100
    assert result.saving_neighbor_amount_krw == 10000
    assert result.regional_amount_percentile_pct == 75
    assert result.field_statuses["regional_usage_percentile_pct"] == (
        "no_data" if value is None else "invalid_response"
    )


async def test_day_rollover_forces_fresh_core_at_next_coordinator_tick(
    hass: HomeAssistant,
    client: MetricsClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC

    import custom_components.kepco_on.power_planner_coordinator as module

    entry = SimpleNamespace(
        entry_id="planner", data={CONF_USERNAME: "PRIVATE"}, async_on_unload=lambda callback: None
    )
    client._authenticated = True
    client.session.cookie_jar.update_cookies({"JSESSIONID": "TEST_SESSION"}, response_url=ORIGIN)
    monkeypatch.setattr(
        module, "datetime", SimpleNamespace(now=lambda zone: client.now.astimezone(UTC))
    )
    coordinator = PowerPlannerCoordinator(
        hass, cast("Any", entry), client, (_house_customer(),), None
    )
    first = await coordinator._async_update_data()
    coordinator.async_set_updated_data(first)
    client.now += timedelta(minutes=20)
    second = await coordinator._async_update_data()
    assert first == second
    assert sum(path == "/rm/getRM0201.do" for path, _ in client.calls) == 1
    client.now = client.now.replace(day=17, hour=0)
    client.current["SELECT_DT"] = "2026.08.17"
    third = await coordinator._async_update_data()
    assert third.snapshots["house-key"].calendar_date == date(2026, 8, 17)
    assert sum(path == "/rm/getRM0201.do" for path, _ in client.calls) == 2


async def test_midnight_does_not_reuse_previous_day_future_placeholders(
    client: MetricsClient,
) -> None:
    client.now = client.now.replace(hour=23, minute=50)
    old = await client._calendar_rows(2026, 8, client.now)
    assert old[16].usage == 0
    client.now += timedelta(minutes=20)
    new = await client._calendar_rows(2026, 8, client.now)
    assert new[16].usage == 16
    assert len(client.calls) == 2
