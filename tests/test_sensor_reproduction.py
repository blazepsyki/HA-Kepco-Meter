"""Check house-contract reproduction against production parsers and entities."""

from __future__ import annotations

import json
from typing import Any, cast

from custom_components.kepco_on.api import KepcoOnClient
from custom_components.kepco_on.const import ENDPOINT_MAIN_CHART, ENDPOINT_POWER_PLANNER
from custom_components.kepco_on.models import KepcoCustomer
from custom_components.kepco_on.parser import parse_house_bill
from tools.reproduce_sensor_values import DemoAuth, demo_report, response_summary, sensor_report


def report_sensors(report: dict[str, object]) -> dict[str, dict[str, Any]]:
    return {sensor["key"]: sensor for sensor in cast("list[dict[str, Any]]", report["sensors"])}


async def test_house_demo_connects_detail_values_without_losing_amount_or_zero() -> None:
    report = await demo_report()
    sensors = report_sensors(report)
    assert len(sensors) == 34
    for key in (
        "current_period_usage",
        "predicted_period_usage",
        "building_average_usage",
        "apartment_average_usage",
    ):
        assert sensors[key]["expected_state"] == "unknown"
        assert sensors[key]["available"] is True
    for key in (
        "base_charge",
        "energy_charge",
        "meter_reading",
        "previous_meter_reading",
        "meter_reading_day",
        "previous_month_usage",
        "previous_month_co2_estimate",
        "last_year_same_month_usage",
        "usage_history_last_year_same_month",
    ):
        assert sensors[key]["value_present"] is True
    assert sensors["amount_due"]["value_present"] is True
    assert sensors["usage_history_previous_month"]["value_present"] is True
    assert sensors["common_usage"]["value_present"] is True
    assert sensors["common_usage"]["is_zero"] is True
    assert report["ha_state_publication_tested"] is False
    assert sum(sensor["value_present"] for sensor in sensors.values()) == 30
    assert report["house_detail_status"] == "ok"


async def test_house_planner_zero_survives_while_prediction_is_withheld() -> None:
    class ZeroPlannerAuth(DemoAuth):
        async def async_protected_request(
            self,
            path: str,
            payload: dict[str, object] | None,
            *,
            submission_id: str | None = None,
        ) -> dict[str, object]:
            if path == ENDPOINT_POWER_PLANNER:
                return {"dma_powerPlanner": {"RETURN_CD": "00", "F_AP_QT": 0, "PREDICT_TOT": 0}}
            return await super().async_protected_request(path, payload, submission_id=submission_id)

    customer = KepcoCustomer(
        stable_key="PRIVATE_STABLE_KEY",
        apartment_name="PRIVATE_ADDRESS",
        dong="",
        ho="",
        contract_method="주택용",
        is_supported=True,
        _customer_number="PRIVATE_CUSTOMER_NUMBER",
        _house_contract_number="PRIVATE_CONTRACT_NUMBER",
    )
    bill = await KepcoOnClient(cast("Any", ZeroPlannerAuth())).async_get_bill(customer)
    report = sensor_report(customer, bill, [], mode="synthetic_house")
    sensors = report_sensors(report)
    assert sensors["current_period_usage"]["is_zero"] is True
    assert sensors["current_period_usage"]["value_present"] is True
    assert sensors["predicted_period_usage"]["value_present"] is False
    assert (
        sensors["predicted_period_usage"]["planner_diagnostics"]["data_status"]
        == "source_unit_unverified"
    )
    serialized = json.dumps(report)
    assert "PRIVATE_" not in serialized


def test_response_metadata_excludes_values_and_unlisted_fields() -> None:
    payload: dict[str, object] = {
        "dlt_chart": [
            {"jojYmFilter": "PRIVATE_VALUE", "kwh": "PRIVATE_VALUE", "token": "PRIVATE_VALUE"}
        ],
        "password": "PRIVATE_VALUE",
    }
    summary = response_summary(ENDPOINT_MAIN_CHART, payload)
    assert "PRIVATE_VALUE" not in json.dumps(summary)
    assert "password" not in json.dumps(summary)
    assert "token" not in json.dumps(summary)


def test_planner_response_metadata_rejects_arbitrary_return_code_text() -> None:
    summary = response_summary(
        ENDPOINT_POWER_PLANNER,
        {"dma_powerPlanner": {"RETURN_CD": "PRIVATE_VALUE", "F_AP_QT": 0}},
    )
    assert summary["return_code_shape"] == "invalid"
    assert "PRIVATE_VALUE" not in json.dumps(summary)


async def test_house_history_populates_monthly_slots_and_previous_usage_fields() -> None:
    auth = DemoAuth()
    payload = await auth.async_protected_request(ENDPOINT_MAIN_CHART, None)
    rows = [
        row
        for row in cast("list[dict[str, object]]", payload["dlt_chart"])
        if str(row["jojYmFilter"]).startswith("2026")
    ]
    payload["dlt_chart"] = [
        {"jojYmFilter": month, "kwh": "100", "afterMny": "10000", "gigan": ""}
        for month in ("202507", "202508", "202509")
    ] + rows
    bill = parse_house_bill(payload)
    customer = KepcoCustomer(
        stable_key="DEMO_CUSTOMER",
        apartment_name="DEMO_HOUSE",
        dong="",
        ho="",
        contract_method="주택용",
        is_supported=True,
        _customer_number="DEMO_CUSTOMER_NUMBER",
        _house_contract_number="DEMO_CONTRACT_NUMBER",
    )
    sensors = report_sensors(sensor_report(customer, bill, [], mode="synthetic_house"))
    for key in (
        "usage_history_last_year_two_months_ago",
        "usage_history_last_year_previous_month",
        "usage_history_last_year_same_month",
    ):
        assert sensors[key]["value_present"] is True
    assert sensors["last_year_same_month_usage"]["value_present"] is True
    assert sensors["previous_month_usage"]["value_present"] is True
