"""Residential direct-contract parsers with synthetic response fixtures."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import cast

import pytest
from custom_components.kepco_on.exceptions import KepcoOnProtocolError
from custom_components.kepco_on.models import KepcoCustomer
from custom_components.kepco_on.parser import (
    parse_customers,
    parse_house_bill,
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


def test_parse_house_bill_derives_previous_and_last_year_usage_from_history() -> None:
    payload = _load("house_main_chart.json")
    rows = cast("list[dict[str, object]]", payload["dlt_chart"])
    rows.append({"jojYmFilter": "202508", "kwh": "512", "afterMny": "120000"})

    bill = parse_house_bill(payload)

    assert bill.bill_month == "202608"
    assert bill.previous_usage_kwh == 457
    assert bill.last_year_usage_kwh == 512


def test_parse_house_bill_leaves_comparison_months_missing_from_history_unknown() -> None:
    payload = _load("house_main_chart.json")
    rows = cast("list[dict[str, object]]", payload["dlt_chart"])
    rows[1]["kwh"] = ""

    bill = parse_house_bill(payload)

    assert bill.previous_usage_kwh is None
    assert bill.last_year_usage_kwh is None


@pytest.mark.asyncio
async def test_house_bill_fills_previous_and_last_year_usage_and_emission_sensors() -> None:
    from .test_sensor import by_key, setup_entities

    payload = _load("house_main_chart.json")
    cast("list[dict[str, object]]", payload["dlt_chart"]).append(
        {"jojYmFilter": "202508", "kwh": "512", "afterMny": "120000"}
    )
    house = KepcoCustomer(
        stable_key="house-key",
        apartment_name="주택용/3kW",
        dong="-",
        ho="-",
        contract_method="주택용/3kW",
        is_supported=True,
        _customer_number="TEST_SI_CUST_001",
        _house_contract_number="TEST_SI_CUST_001",
    )

    sensors = by_key(
        await setup_entities(
            customers=(house,),
            bills_by_customer_key={house.stable_key: parse_house_bill(payload)},
        )
    )

    assert sensors["previous_month_usage"].native_value == 457
    assert sensors["last_year_same_month_usage"].native_value == 512
    # Default factor 0.459 kg CO2/kWh: 457 -> 209.763, 512 -> 235.008.
    assert sensors["previous_month_co2_estimate"].native_value == 210
    assert sensors["last_year_same_month_co2_estimate"].native_value == 235


def test_parse_house_bill_missing_chart_raises() -> None:
    with pytest.raises(KepcoOnProtocolError):
        parse_house_bill({"rsMsg": {}})
