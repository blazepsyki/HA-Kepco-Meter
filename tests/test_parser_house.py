"""Residential direct-contract parsers with synthetic response fixtures."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import cast

import pytest
from custom_components.kepco_on.exceptions import KepcoOnProtocolError
from custom_components.kepco_on.parser import (
    parse_customers,
    parse_house_bill,
    parse_house_bill_detail,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, object]:
    return cast(
        "dict[str, object]",
        json.loads((FIXTURES / name).read_text(encoding="utf-8")),
    )


def test_parse_customers_house_contract() -> None:
    customers = parse_customers(_load("house_customer_list.json"), "uidhash")
    assert len(customers) == 1
    customer = customers[0]
    assert customer.is_house is True
    assert customer.is_supported is True
    assert customer.customer_number == "TEST_SI_CUST_001"
    assert customer.house_contract_number == "TEST_SI_CUST_001"
    assert customer.contract_method == "주택용/3kW"
    assert customer.change_ymd == "20260409"
    assert customer.apartment_name == "주택용/3kW"


def test_parse_customers_house_with_cust_no_still_uses_si_cust_no() -> None:
    payload = _load("house_customer_list.json")
    rows = cast("list[dict[str, object]]", payload["dlt_myPageAppendList"])
    rows[0]["CUST_NO"] = "IGNORED_APARTMENT_STYLE_NUMBER"

    customer = parse_customers(payload, "uidhash")[0]

    assert customer.is_house is True
    assert customer.customer_number == "TEST_SI_CUST_001"
    assert customer.house_contract_number == "TEST_SI_CUST_001"


def test_parse_customers_house_without_si_cust_no_raises() -> None:
    payload = _load("house_customer_list.json")
    rows = payload.get("dlt_myPageAppendList")
    assert isinstance(rows, list)
    row = rows[0]
    assert isinstance(row, dict)
    del row["SI_CUST_NO"]
    with pytest.raises(KepcoOnProtocolError):
        parse_customers(payload, "uidhash")


def test_parse_house_bill_rejects_non_object_rows() -> None:
    payload = _load("house_main_chart.json")
    cast("list[object]", payload["dlt_chart"]).append("not-a-dict")
    with pytest.raises(KepcoOnProtocolError):
        parse_house_bill(payload)


def test_parse_house_bill_requires_month() -> None:
    payload = _load("house_main_chart.json")
    rows = cast("list[dict[str, object]]", payload["dlt_chart"])
    del rows[0]["jojYmFilter"]
    with pytest.raises(KepcoOnProtocolError):
        parse_house_bill(payload)


def test_parse_house_bill_rejects_duplicate_months() -> None:
    payload = _load("house_main_chart.json")
    rows = cast("list[dict[str, object]]", payload["dlt_chart"])
    rows.append(dict(rows[0]))
    with pytest.raises(KepcoOnProtocolError):
        parse_house_bill(payload)


@pytest.mark.parametrize("gigan", [None, "이상한 기간", "2026.02.30-2026.03.08"])
def test_parse_house_bill_tolerates_bad_period(gigan: object) -> None:
    payload = _load("house_main_chart.json")
    rows = cast("list[dict[str, object]]", payload["dlt_chart"])
    latest = max(rows, key=lambda row: str(row["jojYmFilter"]))
    latest["gigan"] = gigan
    bill = parse_house_bill(payload)
    assert bill.period_start is None
    assert bill.period_end is None


def test_parse_house_bill_latest_and_history() -> None:
    bill = parse_house_bill(_load("house_main_chart.json"))
    assert bill.bill_month == "202608"
    assert bill.usage_kwh == 603
    assert bill.amount_krw == 142870
    assert bill.period_start == date(2026, 7, 9)
    assert bill.period_end == date(2026, 8, 8)
    assert [point.month for point in bill.history] == ["202606", "202607", "202608"]
    assert bill.history[0].usage_kwh == 417
    assert bill.charge.base_krw is None
    assert bill.previous_usage_kwh == 457


def test_parse_house_bill_missing_chart_raises() -> None:
    with pytest.raises(KepcoOnProtocolError):
        parse_house_bill({"rsMsg": {}})


def test_house_detail_populates_existing_bill_fields() -> None:
    bill = parse_house_bill_detail(
        _load("house_bill_detail.json"), parse_house_bill(_load("house_main_chart.json"))
    )
    assert bill.house_detail_status == "ok"
    assert bill.meter_reading_day == "09"
    assert bill.current_meter_reading == 12345
    assert bill.previous_meter_reading == 11742
    assert bill.previous_usage_kwh == 457
    assert bill.last_year_usage_kwh == 520
    assert bill.amount_krw == 118810
    assert bill.charge.base_krw == 6060
    assert bill.charge.energy_krw == 100000
    assert bill.charge.climate_krw == 5427
    assert bill.charge.fuel_krw == 3015
    assert bill.charge.child_discount_krw == -10000
    assert bill.charge.subtotal_krw == 104502
    assert bill.charge.vat_krw == 10450
    assert bill.charge.fund_krw == 3860
    assert bill.charge.rounding_krw == -2
    assert len(bill.history) == 3
    assert bill.history[-1].amount_krw == bill.amount_krw


@pytest.mark.parametrize("month", ["07", "09"])
def test_house_detail_rejects_different_month(month: str) -> None:
    payload = _load("house_bill_detail.json")
    cast("dict[str, object]", payload["dma_charge"])["mm"] = month
    with pytest.raises(KepcoOnProtocolError, match="does not match"):
        parse_house_bill_detail(payload, parse_house_bill(_load("house_main_chart.json")))


def test_house_detail_preserves_zero_missing_values_and_history_fallback() -> None:
    payload = _load("house_bill_detail.json")
    detail = cast("dict[str, object]", payload["dma_charge"])
    detail.update({"whmMtrAlw": "0.00", "bfrMtrAlw": "", "befKwh": "", "gumchmDd": 9})
    payload["dlt_mny"] = [
        {"mnyTitle": "알 수 없는 요금 항목", "mnyMny": "not-a-number"},
        {"mnyTitle": "기본요금", "mnyMny": "0"},
    ]
    bill = parse_house_bill_detail(payload, parse_house_bill(_load("house_main_chart.json")))
    assert bill.current_meter_reading == 0
    assert bill.previous_meter_reading is None
    assert bill.previous_usage_kwh == 457
    assert bill.charge.base_krw == 0
    assert bill.charge.vat_krw is None
    assert bill.amount_krw == 142870


@pytest.mark.parametrize("reading", [True, "NaN", "Infinity", "-1", "bad", 10**400])
def test_house_detail_rejects_invalid_meter_readings(reading: object) -> None:
    payload = _load("house_bill_detail.json")
    cast("dict[str, object]", payload["dma_charge"])["whmMtrAlw"] = reading
    with pytest.raises(KepcoOnProtocolError):
        parse_house_bill_detail(payload, parse_house_bill(_load("house_main_chart.json")))


def test_house_detail_rejects_conflicting_charge_aliases() -> None:
    payload = _load("house_bill_detail.json")
    cast("list[dict[str, object]]", payload["dlt_mny"]).append(
        {"mnyTitle": "부가세", "mnyMny": "99"}
    )
    with pytest.raises(KepcoOnProtocolError, match="ambiguous"):
        parse_house_bill_detail(payload, parse_house_bill(_load("house_main_chart.json")))


def test_house_bill_selects_requested_month_and_rejects_absent_month() -> None:
    bill = parse_house_bill(_load("house_main_chart.json"), "202607")
    assert bill.bill_month == "202607"
    assert bill.usage_kwh == 457
    assert bill.previous_usage_kwh == 417
    with pytest.raises(KepcoOnProtocolError, match="unavailable"):
        parse_house_bill(_load("house_main_chart.json"), "202501")


@pytest.mark.parametrize(
    "replacement",
    [
        {"rsMsg": {"statusCode": "E"}},
        {"dma_charge": None},
        {"dma_charge": {"yy": "2026"}},
        {"dlt_mny": "invalid"},
        {"dlt_mny": [None]},
        {"dlt_mny": [{"mnyTitle": None}]},
    ],
)
def test_house_detail_rejects_unsuccessful_or_malformed_payload(
    replacement: dict[str, object],
) -> None:
    payload = _load("house_bill_detail.json")
    payload.update(replacement)
    with pytest.raises(KepcoOnProtocolError):
        parse_house_bill_detail(payload, parse_house_bill(_load("house_main_chart.json")))


@pytest.mark.parametrize("date_value", ["", None, "07.09", "0709"])
def test_house_short_or_empty_detail_date_preserves_chart_period(date_value: object) -> None:
    payload = _load("house_bill_detail.json")
    cast("dict[str, object]", payload["dma_charge"]).update(
        {"fromMmdd": date_value, "toMmdd": date_value, "gumchmDd": None}
    )
    bill = parse_house_bill_detail(payload, parse_house_bill(_load("house_main_chart.json")))
    assert bill.period_start == date(2026, 7, 9)
    assert bill.period_end == date(2026, 8, 8)
    assert bill.meter_reading_day is None


def test_house_detail_rejects_non_text_dates() -> None:
    payload = _load("house_bill_detail.json")
    cast("dict[str, object]", payload["dma_charge"])["fromMmdd"] = True
    with pytest.raises(KepcoOnProtocolError):
        parse_house_bill_detail(payload, parse_house_bill(_load("house_main_chart.json")))
