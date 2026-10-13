"""Residential direct-contract API tests for KEPCO ON."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

import pytest
from custom_components.kepco_on.api import KepcoOnClient
from custom_components.kepco_on.const import (
    ENDPOINT_HOUSE_BILL_DETAIL,
    ENDPOINT_MAIN_CHART,
    ENDPOINT_POWER_PLANNER,
)
from custom_components.kepco_on.exceptions import (
    KepcoOnAuthError,
    KepcoOnConnectionError,
    KepcoOnProtocolError,
    KepcoOnRateLimitError,
    KepcoOnSessionExpired,
)
from custom_components.kepco_on.models import KepcoCustomer
from homeassistant.core import HomeAssistant

from .test_planner_state_publication import publish
from .test_sensor import setup_entities

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict[str, object]:
    return cast(
        "dict[str, object]",
        json.loads((FIXTURES / name).read_text(encoding="utf-8")),
    )


def _house_customer(*, change_ymd: str = "20260409") -> KepcoCustomer:
    return KepcoCustomer(
        stable_key="house-key",
        apartment_name="주택용/3kW",
        dong="",
        ho="",
        contract_method="주택용/3kW",
        is_supported=True,
        _customer_number="TEST_SI_CUST_001",
        _house_contract_number="TEST_SI_CUST_001",
        _change_ymd=change_ymd,
    )


class HouseAuth:
    """Return synthetic endpoint responses and record the business requests."""

    def __init__(
        self, responses: dict[str, dict[str, object] | BaseException] | None = None
    ) -> None:
        self.responses: dict[str, dict[str, object] | BaseException] = {
            ENDPOINT_MAIN_CHART: _load_fixture("house_main_chart.json"),
            ENDPOINT_HOUSE_BILL_DETAIL: _load_fixture("house_bill_detail.json"),
            ENDPOINT_POWER_PLANNER: _load_fixture("house_power_planner.json"),
        }
        self.responses.update(responses or {})
        self.calls: list[tuple[str, dict[str, object] | None, str | None]] = []

    def account_uid_hash(self) -> str:
        return "synthetic-account"

    async def async_protected_request(
        self, path: str, payload: dict[str, object] | None, *, submission_id: str | None = None
    ) -> dict[str, object]:
        self.calls.append((path, payload, submission_id))
        response = self.responses[path]
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.mark.parametrize("change_ymd", ["20260409", ""])
async def test_house_bill_combines_history_detail_and_current_planner(change_ymd: str) -> None:
    auth = HouseAuth()
    bill = await KepcoOnClient(auth).async_get_bill(_house_customer(change_ymd=change_ymd))
    assert bill.bill_month == "202608"
    assert bill.usage_kwh == 603
    assert bill.amount_krw == 118810
    assert bill.charge.base_krw == 6060
    assert bill.current_meter_reading == 12345
    assert bill.previous_usage_kwh == 457
    assert bill.last_year_usage_kwh == 520
    assert bill.house_detail_status == "ok"
    assert bill.current_period_usage_kwh == pytest.approx(509.783)
    assert bill.predicted_period_usage_kwh is None
    assert [call[0] for call in auth.calls] == [
        ENDPOINT_MAIN_CHART,
        ENDPOINT_HOUSE_BILL_DETAIL,
        ENDPOINT_POWER_PLANNER,
    ]
    for (path, payload, submission_id), expected_id in zip(
        auth.calls,
        (
            "mf_wfm_layout_sbm_houseChart",
            "mf_wfm_layout_sbm_select",
            "mf_wfm_layout_sbm_powerPlanner",
        ),
        strict=True,
    ):
        assert submission_id == expected_id
        assert payload is not None
        search = cast("dict[str, object]", payload["dma_search"])
        assert search["custNo"] == "TEST_SI_CUST_001"
        assert search["chgYmd"] == (change_ymd[:6] if path == ENDPOINT_MAIN_CHART else change_ymd)
        if path != ENDPOINT_POWER_PLANNER:
            assert search["months"] == "24"
        if path == ENDPOINT_HOUSE_BILL_DETAIL:
            assert search["schYm"] == "202608"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (KepcoOnConnectionError("synthetic failure"), "connection_error"),
        (KepcoOnRateLimitError("synthetic failure"), "rate_limited"),
        (KepcoOnProtocolError("synthetic failure"), "invalid_response"),
    ],
)
@pytest.mark.parametrize("endpoint", [ENDPOINT_HOUSE_BILL_DETAIL, ENDPOINT_POWER_PLANNER])
async def test_optional_failure_preserves_billing(
    endpoint: str, error: Exception, status: str
) -> None:
    bill = await KepcoOnClient(HouseAuth({endpoint: error})).async_get_bill(_house_customer())
    assert bill.usage_kwh == 603
    assert bill.previous_usage_kwh == 457
    assert len(bill.history) == 3
    if endpoint == ENDPOINT_HOUSE_BILL_DETAIL:
        assert bill.house_detail_status == status
        assert bill.amount_krw == 142870
        assert bill.charge.base_krw is None
    else:
        assert bill.power_planner_status == status
        assert bill.amount_krw == 118810
        assert bill.charge.base_krw == 6060


async def test_missing_planner_value_preserves_detail() -> None:
    bill = await KepcoOnClient(HouseAuth({ENDPOINT_POWER_PLANNER: {}})).async_get_bill(
        _house_customer()
    )
    assert bill.current_period_usage_kwh is None
    assert bill.predicted_period_usage_kwh is None
    assert bill.house_detail_status == "ok"
    assert bill.amount_krw == 118810


@pytest.mark.parametrize("endpoint", [ENDPOINT_HOUSE_BILL_DETAIL, ENDPOINT_POWER_PLANNER])
@pytest.mark.parametrize(
    "error", [KepcoOnAuthError("auth"), KepcoOnSessionExpired("auth"), asyncio.CancelledError()]
)
async def test_authentication_and_cancellation_propagate(
    endpoint: str, error: BaseException
) -> None:
    with pytest.raises(type(error)):
        await KepcoOnClient(HouseAuth({endpoint: error})).async_get_bill(_house_customer())


async def test_house_requested_month_uses_matching_detail_without_current_planner() -> None:
    detail = _load_fixture("house_bill_detail.json")
    cast("dict[str, object]", detail["dma_charge"]).update({"mm": "07", "kwh": "457"})
    auth = HouseAuth({ENDPOINT_HOUSE_BILL_DETAIL: detail})
    bill = await KepcoOnClient(auth).async_get_bill(_house_customer(), "202607")
    assert bill.bill_month == "202607"
    assert bill.usage_kwh == 457
    assert bill.current_period_usage_kwh is None
    assert bill.house_detail_status == "ok"
    assert [call[0] for call in auth.calls] == [ENDPOINT_MAIN_CHART, ENDPOINT_HOUSE_BILL_DETAIL]
    for _, payload, _ in auth.calls:
        assert payload is not None
        assert cast("dict[str, object]", payload["dma_search"])["schYm"] == "202607"


async def test_house_invalid_month_is_rejected_before_network_request() -> None:
    auth = HouseAuth()
    with pytest.raises(KepcoOnProtocolError):
        await KepcoOnClient(auth).async_get_bill(_house_customer(), "invalid")
    assert auth.calls == []


@pytest.mark.parametrize("detail_available", [True, False])
async def test_house_bill_publishes_existing_sensor_values(
    hass: HomeAssistant, detail_available: bool
) -> None:
    overrides: dict[str, dict[str, object] | BaseException] = (
        {} if detail_available else {ENDPOINT_HOUSE_BILL_DETAIL: {}}
    )
    customer = _house_customer()
    bill = await KepcoOnClient(HouseAuth(overrides)).async_get_bill(customer)
    expected = {
        "monthly_usage": 603,
        "previous_month_usage": 457,
        "base_charge": 6060 if detail_available else None,
        "meter_reading": 12345 if detail_available else None,
        "last_year_same_month_usage": 520 if detail_available else None,
        "co2_estimate": 277,
    }
    sensors = await publish(
        hass,
        await setup_entities(
            customers=(customer,), bills_by_customer_key={customer.stable_key: bill}
        ),
        keys=tuple(expected),
    )
    for key, value in expected.items():
        state = hass.states.get(sensors[key].entity_id)
        assert state is not None
        assert state.attributes["house_detail_status"] == (
            "ok" if detail_available else "invalid_response"
        )
        if value is None:
            assert state.state == "unknown"
        else:
            assert float(state.state) == pytest.approx(value)
