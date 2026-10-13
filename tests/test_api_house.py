"""Residential direct-contract API tests for KEPCO ON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

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


@pytest.mark.asyncio
async def test_house_bill_combines_main_chart_and_power_planner() -> None:
    calls: list[tuple[str, dict[str, object] | None, str | None]] = []

    async def protected_request(
        path: str,
        payload: dict[str, object] | None,
        *,
        submission_id: str | None = None,
    ) -> dict[str, object]:
        calls.append((path, payload, submission_id))
        if path == ENDPOINT_MAIN_CHART:
            return _load_fixture("house_main_chart.json")
        if path == ENDPOINT_HOUSE_BILL_DETAIL:
            return _load_fixture("house_bill_detail.json")
        assert path == ENDPOINT_POWER_PLANNER
        return _load_fixture("house_power_planner.json")

    class Auth:
        async_protected_request = staticmethod(protected_request)

        def account_uid_hash(self) -> str:
            return "ACCOUNT_HASH"

    bill = await KepcoOnClient(cast("Any", Auth())).async_get_bill(
        _house_customer(),
    )

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
    assert calls == [
        (
            ENDPOINT_MAIN_CHART,
            {
                "dma_search": {
                    "schYm": "",
                    "custNo": "TEST_SI_CUST_001",
                    "gubun": "",
                    "schChart": "12",
                    "CUST_NO": "",
                    "housCntrNo": "",
                    "yyyymm": "",
                    "searchType": "",
                    "dong": "",
                    "ho": "",
                    "months": "24",
                    "chgYmd": "202604",
                }
            },
            "mf_wfm_layout_sbm_houseChart",
        ),
        (
            ENDPOINT_HOUSE_BILL_DETAIL,
            {
                "dma_search": {
                    "schYm": "202608",
                    "custNo": "TEST_SI_CUST_001",
                    "schChart": "12",
                    "apKind": "",
                    "userEmailAddr": "",
                    "userMTel": "",
                    "months": "24",
                    "chgYmd": "20260409",
                }
            },
            "mf_wfm_layout_sbm_select",
        ),
        (
            ENDPOINT_POWER_PLANNER,
            {
                "dma_search": {
                    "schYm": "",
                    "custNo": "TEST_SI_CUST_001",
                    "gubun": "",
                    "schChart": "12",
                    "CUST_NO": "",
                    "housCntrNo": "",
                    "yyyymm": "",
                    "searchType": "",
                    "dong": "",
                    "ho": "",
                    "months": "",
                    "chgYmd": "20260409",
                }
            },
            "mf_wfm_layout_sbm_powerPlanner",
        ),
    ]


@pytest.mark.asyncio
async def test_house_bill_keeps_history_when_power_planner_has_no_values() -> None:
    calls: list[tuple[str, dict[str, object] | None, str | None]] = []

    async def protected_request(
        path: str,
        payload: dict[str, object] | None,
        *,
        submission_id: str | None = None,
    ) -> dict[str, object]:
        calls.append((path, payload, submission_id))
        if path == ENDPOINT_MAIN_CHART:
            return _load_fixture("house_main_chart.json")
        if path == ENDPOINT_HOUSE_BILL_DETAIL:
            return _load_fixture("house_bill_detail.json")
        assert path == ENDPOINT_POWER_PLANNER
        return {"rsMsg": {}}

    class Auth:
        async_protected_request = staticmethod(protected_request)

        def account_uid_hash(self) -> str:
            return "ACCOUNT_HASH"

    bill = await KepcoOnClient(cast("Any", Auth())).async_get_bill(_house_customer(change_ymd=""))

    assert bill.bill_month == "202608"
    assert bill.current_period_usage_kwh is None
    assert bill.predicted_period_usage_kwh is None
    assert len(calls) == 3
    main_payload = calls[0][1]
    assert isinstance(main_payload, dict)
    main_search = main_payload.get("dma_search")
    assert isinstance(main_search, dict)
    assert main_search["chgYmd"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [KepcoOnConnectionError, KepcoOnProtocolError, KepcoOnRateLimitError],
)
async def test_house_bill_keeps_history_when_power_planner_fails(
    error_type: type[Exception],
) -> None:
    calls: list[str] = []

    async def protected_request(
        path: str,
        payload: dict[str, object] | None,
        *,
        submission_id: str | None = None,
    ) -> dict[str, object]:
        del payload, submission_id
        calls.append(path)
        if path == ENDPOINT_MAIN_CHART:
            return _load_fixture("house_main_chart.json")
        if path == ENDPOINT_HOUSE_BILL_DETAIL:
            return _load_fixture("house_bill_detail.json")
        assert path == ENDPOINT_POWER_PLANNER
        raise error_type("simulated optional Power Planner failure")

    class Auth:
        async_protected_request = staticmethod(protected_request)

        def account_uid_hash(self) -> str:
            return "ACCOUNT_HASH"

    bill = await KepcoOnClient(cast("Any", Auth())).async_get_bill(_house_customer())

    assert bill.bill_month == "202608"
    assert bill.usage_kwh == 603
    assert bill.current_period_usage_kwh is None
    assert bill.predicted_period_usage_kwh is None
    assert calls == [ENDPOINT_MAIN_CHART, ENDPOINT_HOUSE_BILL_DETAIL, ENDPOINT_POWER_PLANNER]


@pytest.mark.parametrize(
    ("error_type", "expected_status"),
    [
        (KepcoOnConnectionError, "connection_error"),
        (KepcoOnRateLimitError, "rate_limited"),
        (KepcoOnProtocolError, "invalid_response"),
    ],
)
async def test_house_detail_failure_preserves_chart_and_records_status(
    error_type: type[Exception], expected_status: str
) -> None:
    async def request(
        path: str, payload: dict[str, object] | None, *, submission_id: str | None = None
    ) -> dict[str, object]:
        del payload, submission_id
        if path == ENDPOINT_HOUSE_BILL_DETAIL:
            raise error_type("synthetic detail failure")
        return _load_fixture(
            "house_main_chart.json" if path == ENDPOINT_MAIN_CHART else "house_power_planner.json"
        )

    class Auth:
        async_protected_request = staticmethod(request)

    bill = await KepcoOnClient(cast("Any", Auth())).async_get_bill(_house_customer())
    assert bill.house_detail_status == expected_status
    assert bill.usage_kwh == 603
    assert bill.amount_krw == 142870
    assert bill.previous_usage_kwh == 457
    assert bill.charge.base_krw is None


@pytest.mark.parametrize("error_type", [KepcoOnAuthError, KepcoOnSessionExpired])
async def test_house_detail_auth_errors_propagate(error_type: type[Exception]) -> None:
    async def request(
        path: str, payload: dict[str, object] | None, *, submission_id: str | None = None
    ) -> dict[str, object]:
        del payload, submission_id
        if path == ENDPOINT_MAIN_CHART:
            return _load_fixture("house_main_chart.json")
        raise error_type("synthetic auth failure")

    class Auth:
        async_protected_request = staticmethod(request)

    with pytest.raises(error_type):
        await KepcoOnClient(cast("Any", Auth())).async_get_bill(_house_customer())


async def test_house_requested_month_uses_matching_detail_without_current_planner() -> None:
    calls: list[str] = []

    async def request(
        path: str, payload: dict[str, object] | None, *, submission_id: str | None = None
    ) -> dict[str, object]:
        del submission_id
        calls.append(path)
        if path == ENDPOINT_MAIN_CHART:
            return _load_fixture("house_main_chart.json")
        assert path == ENDPOINT_HOUSE_BILL_DETAIL
        assert (
            cast("dict[str, object]", cast("dict[str, object]", payload)["dma_search"])["schYm"]
            == "202607"
        )
        result = _load_fixture("house_bill_detail.json")
        cast("dict[str, object]", result["dma_charge"]).update(
            {"mm": "07", "kwh": "457", "befKwh": "417"}
        )
        return result

    class Auth:
        async_protected_request = staticmethod(request)

    bill = await KepcoOnClient(cast("Any", Auth())).async_get_bill(_house_customer(), "202607")
    assert bill.bill_month == "202607"
    assert bill.usage_kwh == 457
    assert bill.previous_usage_kwh == 417
    assert bill.power_planner_status == "not_requested"
    assert calls == [ENDPOINT_MAIN_CHART, ENDPOINT_HOUSE_BILL_DETAIL]


async def test_house_invalid_month_is_rejected_before_network_request() -> None:
    with pytest.raises(KepcoOnProtocolError):
        await KepcoOnClient(cast("Any", object())).async_get_bill(_house_customer(), "invalid")
