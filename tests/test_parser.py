"""Parser contract tests for sanitized KEPCO ON payload fixtures."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from custom_components.kepco_on import parser
from custom_components.kepco_on.exceptions import KepcoOnNoCustomersError, KepcoOnProtocolError
from custom_components.kepco_on.models import KepcoCoordinatorData
from custom_components.kepco_on.parser import (
    CustomerRowSkips,
    parse_bill,
    parse_customers,
    parse_customers_with_skips,
    parse_date,
    parse_int,
    parse_year_month,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


def load_fixture(name: str) -> dict[str, object]:
    return cast("dict[str, object]", json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def as_object_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("96,330", 96330),
        ("-16000", -16000),
        (0, 0),
        (None, None),
        ("", None),
        ("null", None),
    ],
)
def test_parse_int_accepts_kepco_number_shapes(value: object, expected: int | None) -> None:
    assert parse_int(value, "amount") == expected


def test_parse_int_rejects_nonempty_invalid_value() -> None:
    with pytest.raises(KepcoOnProtocolError):
        parse_int("not-a-number", "amount")


@pytest.mark.parametrize("value", [True, object()])
def test_parse_int_rejects_bool_and_non_string_values(value: object) -> None:
    with pytest.raises(KepcoOnProtocolError):
        parse_int(value, "amount")


@pytest.mark.parametrize(
    "value",
    ["1,2,3", "\uff11", "\uff11\uff12\uff13", "+123", "--123", "123-"],
)
def test_parse_int_rejects_non_ascii_and_bad_grouping(value: str) -> None:
    with pytest.raises(KepcoOnProtocolError):
        parse_int(value, "amount")


def test_parse_date_is_strict() -> None:
    assert parse_date("20260731", "period_end") == date(2026, 7, 31)

    with pytest.raises(KepcoOnProtocolError):
        parse_date("20260230", "period_end")


@pytest.mark.parametrize(("value", "expected"), [(None, None), ("", None), ("null", None)])
def test_parse_date_accepts_empty_kepco_values(value: object, expected: date | None) -> None:
    assert parse_date(value, "period_end") == expected


def test_parse_date_rejects_non_string_values() -> None:
    with pytest.raises(KepcoOnProtocolError):
        parse_date(20260731, "period_end")


def test_parse_year_month_is_strict() -> None:
    assert parse_year_month("202607", "bill_month") == "202607"

    with pytest.raises(KepcoOnProtocolError):
        parse_year_month("202613", "bill_month")


@pytest.mark.parametrize(("value", "expected"), [(None, None), ("", None), ("null", None)])
def test_parse_year_month_accepts_empty_kepco_values(value: object, expected: str | None) -> None:
    assert parse_year_month(value, "bill_month") == expected


def test_parse_year_month_rejects_non_string_values() -> None:
    with pytest.raises(KepcoOnProtocolError):
        parse_year_month(202607, "bill_month")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("01", "01"), ("31", "31"), ("", None), ("null", None), (None, None)],
)
def test_parse_day_of_month_accepts_kepco_day_shapes(value: object, expected: str | None) -> None:
    assert parser.parse_day_of_month(value, "meter_reading_day") == expected


@pytest.mark.parametrize("value", ["00", "32", "1", " 01 ", 1, "\uff10\uff11"])
def test_parse_day_of_month_rejects_invalid_protocol_values(value: object) -> None:
    with pytest.raises(KepcoOnProtocolError):
        parser.parse_day_of_month(value, "meter_reading_day")


@pytest.mark.parametrize("value", ["\uff12\uff10\uff12\uff16\uff10\uff17", "2026\uff107"])
def test_parse_year_month_rejects_unicode_digits(value: str) -> None:
    with pytest.raises(KepcoOnProtocolError):
        parse_year_month(value, "bill_month")


@pytest.mark.parametrize(
    "value",
    ["\uff12\uff10\uff12\uff16\uff10\uff17\uff13\uff11", "202607\uff131"],
)
def test_parse_date_rejects_unicode_digits(value: str) -> None:
    with pytest.raises(KepcoOnProtocolError):
        parse_date(value, "period_end")


def test_parse_customers_accepts_single_customer_append_list() -> None:
    customers = parse_customers(load_fixture("customer_list_single.json"), "ACCOUNT_HASH")

    assert len(customers) == 1
    customer = customers[0]
    assert customer.customer_number == "TEST_CUST_001"
    assert customer.house_contract_number == "TEST_HOUSE_001"
    assert customer.apartment_name == "TEST_APT_001"
    assert customer.dong == "1001"
    assert customer.ho == "0101"
    assert customer.contract_method == "아파트(단일계약)"
    assert customer.is_supported is True


def test_parse_customers_accepts_multiple_my_page_append_list() -> None:
    customers = parse_customers(load_fixture("customer_list_multiple.json"), "ACCOUNT_HASH")

    assert [customer.customer_number for customer in customers] == [
        "TEST_CUST_001",
        "TEST_CUST_002",
    ]
    assert len({customer.stable_key for customer in customers}) == len(customers)
    assert customers[0].stable_key != customers[1].stable_key


def test_parse_customers_skips_unsupported_and_malformed_rows_in_mixed_account(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.kepco_on.parser")
    alone = parse_customers(load_fixture("customer_list_single.json"), "ACCOUNT_HASH")[0]
    house_row = cast(
        "list[dict[str, object]]", load_fixture("house_customer_list.json")["dlt_myPageAppendList"]
    )[0]
    apartment_row = cast(
        "list[dict[str, object]]", load_fixture("customer_list_single.json")["dlt_appendList"]
    )[0]
    payload: dict[str, object] = {
        "dlt_myPageAppendList": [
            {
                "CUST_NO": "TEST_SKIPPED_GENERAL_CUST",
                "SI_CUST_NO": "TEST_SKIPPED_GENERAL_HOUSE",
                "cntrMthdCd": "일반용(갑)저압",
            },
            apartment_row,
            {"SI_CUST_NO": "TEST_SKIPPED_MISSING_CUST", "cntrMthdCd": "아파트(단일계약)"},
            {"CUST_NO": "TEST_SKIPPED_BAD_TYPE", "SI_CUST_NO": "TEST_X", "cntrMthdCd": 1},
            "not-an-object",
            {"cntrMthdCd": "주택용/3kW"},
            house_row,
        ]
    }

    customers = parse_customers(payload, "ACCOUNT_HASH")

    assert [customer.customer_number for customer in customers] == [
        "TEST_CUST_001",
        "TEST_SI_CUST_001",
    ]
    assert customers[0] == alone
    assert customers[1].is_house is True
    assert "Skipped 1 KEPCO ON customer row(s) with unsupported contract types" in caplog.text
    assert "Skipped 4 malformed KEPCO ON customer row(s)" in caplog.text
    assert "TEST_SKIPPED" not in caplog.text
    assert "TEST_CUST_001" not in caplog.text

    with_skips, skips = parse_customers_with_skips(payload, "ACCOUNT_HASH")

    assert with_skips == customers
    assert skips == CustomerRowSkips(unsupported=1, malformed=4)
    assert parse_customers_with_skips(
        load_fixture("customer_list_single.json"), "ACCOUNT_HASH"
    ) == ((alone,), CustomerRowSkips())


@pytest.mark.asyncio
async def test_client_records_skipped_customer_row_counts_for_the_config_flow() -> None:
    from custom_components.kepco_on.api import KepcoOnClient

    apartment_row = cast(
        "list[dict[str, object]]", load_fixture("customer_list_single.json")["dlt_appendList"]
    )[0]
    payload: dict[str, object] = {
        "dlt_myPageAppendList": [
            apartment_row,
            {"SI_CUST_NO": "TEST_SKIPPED_MISSING_CUST", "cntrMthdCd": "아파트(단일계약)"},
            {"CUST_NO": "TEST_X", "SI_CUST_NO": "TEST_Y", "cntrMthdCd": "일반용(갑)저압"},
        ]
    }

    class Auth:
        async def async_protected_request(
            self, path: str, body: object, *, submission_id: str | None = None
        ) -> dict[str, object]:
            del path, body, submission_id
            return payload

        def account_uid_hash(self) -> str:
            return "ACCOUNT_HASH"

    client = KepcoOnClient(cast("Any", Auth()))
    assert client.customer_row_skips == CustomerRowSkips()

    customers = await client.async_get_customers()

    assert [customer.customer_number for customer in customers] == ["TEST_CUST_001"]
    assert client.customer_row_skips == CustomerRowSkips(unsupported=1, malformed=1)


def test_parse_customers_keeps_fallback_names_numbered_by_accepted_customers() -> None:
    payload: dict[str, object] = {
        "dlt_myPageAppendList": [
            {"CUST_NO": "TEST_CUST_X", "SI_CUST_NO": "TEST_HOUSE_X", "cntrMthdCd": "산업용"},
            {
                "CUST_NO": "TEST_CUST_001",
                "SI_CUST_NO": "TEST_HOUSE_001",
                "cntrMthdCd": "아파트(종합계약)",
            },
        ]
    }

    customers = parse_customers(payload, "ACCOUNT_HASH")

    assert [customer.apartment_name for customer in customers] == ["한전ON 고객 1"]


def test_parse_customers_rejects_list_without_any_supported_row() -> None:
    payload: dict[str, object] = {
        "dlt_myPageAppendList": [
            {"CUST_NO": "TEST_CUST_X", "SI_CUST_NO": "TEST_HOUSE_X", "cntrMthdCd": "산업용"},
            {"SI_CUST_NO": "TEST_HOUSE_Y", "cntrMthdCd": "아파트(단일계약)"},
        ]
    }

    with pytest.raises(KepcoOnProtocolError) as raised:
        parse_customers(payload, "ACCOUNT_HASH")

    assert "TEST_" not in str(raised.value)


def test_parse_customers_accepts_customer_number_list_without_display_fields() -> None:
    customers = parse_customers(
        {
            "dlt_myPageAppendList": [
                {
                    "CUST_NO": "TEST_CUST_001",
                    "SI_CUST_NO": "TEST_HOUSE_001",
                    "cntrMthdCd": "아파트(단일계약)",
                }
            ]
        },
        "ACCOUNT_HASH",
    )

    assert len(customers) == 1
    customer = customers[0]
    assert customer.customer_number == "TEST_CUST_001"
    assert customer.house_contract_number == "TEST_HOUSE_001"
    assert customer.apartment_name == "한전ON 고객 1"
    assert customer.dong == "미확인"
    assert customer.ho == "미확인"


def test_parse_customers_rejects_empty_list() -> None:
    with pytest.raises(KepcoOnNoCustomersError):
        parse_customers({"dlt_appendList": []}, "ACCOUNT_HASH")


@pytest.mark.parametrize(
    "payload",
    [
        {"dlt_appendList": "not-list"},
        {"dlt_appendList": ["not-object"]},
        {},
    ],
)
def test_parse_customers_rejects_malformed_customer_list_shapes(
    payload: dict[str, object],
) -> None:
    with pytest.raises(KepcoOnProtocolError):
        parse_customers(payload, "ACCOUNT_HASH")


@pytest.mark.parametrize(
    "row_update",
    [
        {"CUST_NO": ""},
        {"SI_CUST_NO": ""},
        {"cntrMthdCd": 1},
        {"cntrMthdCd": "other"},
    ],
)
def test_parse_customers_rejects_missing_or_unsupported_customer_fields(
    row_update: dict[str, object],
) -> None:
    payload = load_fixture("customer_list_single.json")
    row = as_object_dict(cast(list[object], payload["dlt_appendList"])[0])
    row.update(row_update)

    with pytest.raises(KepcoOnProtocolError):
        parse_customers(payload, "ACCOUNT_HASH")


@pytest.mark.parametrize(
    "row_update",
    [
        {"APT_NAME": 1},
        {"APT_DONGNO": 1},
        {"APT_HONO": 1},
    ],
)
def test_parse_customers_rejects_invalid_display_field_types(row_update: dict[str, object]) -> None:
    payload = load_fixture("customer_list_single.json")
    row = as_object_dict(cast(list[object], payload["dlt_appendList"])[0])
    row.update(row_update)

    with pytest.raises(KepcoOnProtocolError):
        parse_customers(payload, "ACCOUNT_HASH")


def test_parse_customers_ignores_unproven_display_aliases() -> None:
    payload = load_fixture("customer_list_single.json")
    row = as_object_dict(cast(list[object], payload["dlt_appendList"])[0])
    row.pop("APT_NAME")
    row.pop("APT_DONGNO")
    row.pop("APT_HONO")
    row.update({"APT_NM": "TEST_APT_ALIAS", "DONG_NO": "1001", "HO_NO": "0101"})

    customer = parse_customers(payload, "ACCOUNT_HASH")[0]

    assert customer.apartment_name == "한전ON 고객 1"
    assert customer.dong == "미확인"
    assert customer.ho == "미확인"


def test_parse_customer_repr_does_not_expose_raw_identifiers() -> None:
    customer = parse_customers(load_fixture("customer_list_single.json"), "ACCOUNT_HASH")[0]

    rendered = repr(customer)
    assert "TEST_CUST_001" not in rendered
    assert "TEST_HOUSE_001" not in rendered
    assert customer.stable_key
    assert "TEST_CUST_001" not in customer.stable_key
    assert "TEST_HOUSE_001" not in customer.stable_key


def test_customer_stable_key_is_full_domain_separated_sha256() -> None:
    customer = parse_customers(load_fixture("customer_list_single.json"), "ACCOUNT_HASH")[0]
    same_customer = parse_customers(load_fixture("customer_list_single.json"), "ACCOUNT_HASH")[0]
    other_account = parse_customers(
        load_fixture("customer_list_single.json"), "OTHER_ACCOUNT_HASH"
    )[0]

    assert len(customer.stable_key) == 64
    assert all(character in "0123456789abcdef" for character in customer.stable_key)
    assert customer.stable_key == same_customer.stable_key
    assert customer.stable_key != other_account.stable_key
    assert "ACCOUNT_HASH" not in customer.stable_key
    assert "TEST_CUST_001" not in customer.stable_key
    assert "TEST_HOUSE_001" not in customer.stable_key


def test_parse_latest_bill_extracts_all_governing_values() -> None:
    bill = parse_bill(load_fixture("bill_latest.json"), requested_month=None)

    assert bill.bill_month == "202608"
    assert bill.period_start == date(2026, 7, 1)
    assert bill.period_end == date(2026, 7, 31)
    assert bill.usage_kwh == 573
    assert bill.household_usage_kwh is None
    assert bill.common_usage_kwh is None
    assert bill.previous_usage_kwh == 406
    assert bill.last_year_usage_kwh == 612
    assert bill.building_average_kwh == 363
    assert bill.apartment_average_kwh == 284
    assert bill.current_meter_reading == 23139
    assert bill.previous_meter_reading == 22566
    assert bill.meter_reading_day == "01"
    assert bill.amount_krw == 96330
    assert bill.charge.subtotal_krw == 85484
    assert bill.charge.base_krw == 6060
    assert bill.charge.energy_krw == 87402
    assert bill.charge.climate_krw == 5157
    assert bill.charge.fuel_krw == 2865
    assert bill.charge.child_discount_krw == -16000
    assert bill.charge.vat_krw == 8548
    assert bill.charge.fund_krw == 2300
    assert bill.charge.rounding_krw == 2
    assert len(bill.history) == 24
    assert bill.history[0].month == "202409"
    assert bill.history[-1].month == "202608"


def test_parse_requested_bill_uses_requested_month_over_response_month() -> None:
    bill = parse_bill(load_fixture("bill_202607.json"), requested_month="202607")

    assert bill.bill_month == "202607"
    assert bill.response_bill_month == "202608"
    assert bill.period_start == date(2026, 6, 1)
    assert bill.period_end == date(2026, 6, 30)
    assert bill.usage_kwh == 406
    assert bill.household_usage_kwh is None
    assert bill.common_usage_kwh is None
    assert bill.previous_usage_kwh == 371
    assert bill.last_year_usage_kwh == 459
    assert bill.building_average_kwh == 248
    assert bill.apartment_average_kwh == 185
    assert bill.current_meter_reading == 22566
    assert bill.previous_meter_reading == 22160
    assert bill.meter_reading_day == "01"
    assert bill.amount_krw == 59720
    assert bill.charge.subtotal_krw == 52997
    assert bill.charge.base_krw == 6060
    assert bill.charge.energy_krw == 57253
    assert bill.charge.climate_krw == 3654
    assert bill.charge.fuel_krw == 2030
    assert bill.charge.child_discount_krw == -16000
    assert bill.charge.vat_krw == 5300
    assert bill.charge.fund_krw == 1430
    assert bill.charge.rounding_krw == 7
    assert bill.history[0].month == "202408"
    assert bill.history[-1].month == "202607"


def test_parse_bill_extracts_explicit_household_and_common_usage() -> None:
    payload = load_fixture("bill_latest.json")
    result = as_object_dict(payload["dma_result"])
    result["DO_TP_RSD_KWH"] = "560"
    result["DO_TP_CMNUS_KWH"] = "13"

    parsed = parse_bill(payload, requested_month=None)

    assert parsed.household_usage_kwh == 560
    assert parsed.common_usage_kwh == 13


def test_parse_bill_status_success_accepts_hxi001() -> None:
    bill = parse_bill(
        {
            "rsMsg": {"statusCode": "S"},
            "dma_result": {"DO_ERR_CODE": "HXI001", "DO_BILL_YM": "202608"},
        },
        None,
    )

    assert bill.bill_month == "202608"


@pytest.mark.parametrize(
    "payload",
    [
        {"DO_ERR_CODE": "HXI001", "DO_BILL_YM": "202608"},
        {"status": "S", "DO_ERR_CODE": "HXI001", "DO_BILL_YM": "202608"},
        {
            "rsMsg": {"statusCode": "F"},
            "dma_result": {"DO_ERR_CODE": "HXI001", "DO_BILL_YM": "202608"},
        },
        {
            "status": "S",
            "rsMsg": {"statusCode": "F"},
            "dma_result": {"DO_ERR_CODE": "HXI001", "DO_BILL_YM": "202608"},
        },
    ],
)
def test_parse_bill_requires_successful_rsmsg_status(payload: dict[str, object]) -> None:
    with pytest.raises(KepcoOnProtocolError):
        parse_bill(payload, None)


def test_parse_bill_requires_object_dma_result() -> None:
    with pytest.raises(KepcoOnProtocolError):
        parse_bill({"rsMsg": {"statusCode": "S"}, "dma_result": []}, None)


def test_parse_bill_requires_effective_month_from_request_or_response() -> None:
    with pytest.raises(KepcoOnProtocolError):
        parse_bill({"rsMsg": {"statusCode": "S"}, "dma_result": {}}, None)


def test_parse_bill_history_must_be_sorted_unique_and_valid() -> None:
    base = load_fixture("bill_latest.json")

    duplicate = dict(base)
    duplicate["dlt_chrtList"] = [{"DO_CHRT_REQ_YM": "202607"}, {"DO_CHRT_REQ_YM": "202607"}]
    with pytest.raises(KepcoOnProtocolError):
        parse_bill(duplicate, None)

    unsorted = dict(base)
    unsorted["dlt_chrtList"] = [{"DO_CHRT_REQ_YM": "202608"}, {"DO_CHRT_REQ_YM": "202607"}]
    with pytest.raises(KepcoOnProtocolError):
        parse_bill(unsorted, None)

    invalid = dict(base)
    invalid["dlt_chrtList"] = [{"DO_CHRT_REQ_YM": "20260230"}]
    with pytest.raises(KepcoOnProtocolError):
        parse_bill(invalid, None)


@pytest.mark.parametrize(
    "history",
    [
        "not-list",
        ["not-object"],
        [{}],
        [{"DO_CHRT_REQ_YM": "202608"}, {"DO_CHRT_REQ_YM": "202607"}],
    ],
)
def test_parse_bill_rejects_malformed_history_shapes(history: object) -> None:
    payload = {
        "rsMsg": {"statusCode": "S"},
        "dma_result": {"DO_ERR_CODE": "HXI001", "DO_BILL_YM": "202608"},
        "dlt_chrtList": history,
    }

    with pytest.raises(KepcoOnProtocolError):
        parse_bill(payload, None)


def test_parse_bill_rejects_large_response_history_month_contradiction() -> None:
    payload = load_fixture("bill_202607.json")
    result = as_object_dict(payload["dma_result"])
    result["DO_BILL_YM"] = "202701"

    with pytest.raises(KepcoOnProtocolError):
        parse_bill(payload, requested_month="202607")


def test_parse_bill_rejects_history_range_that_misses_effective_month() -> None:
    payload: dict[str, object] = {
        "rsMsg": {"statusCode": "S"},
        "dma_result": {"DO_ERR_CODE": "HXI001", "DO_BILL_YM": "202608"},
        "dlt_chrtList": [{"DO_CHRT_REQ_YM": "202607"}],
    }

    with pytest.raises(KepcoOnProtocolError):
        parse_bill(payload, requested_month=None)


def test_parse_bill_accepts_december_history_with_next_january_response_month() -> None:
    payload: dict[str, object] = {
        "rsMsg": {"statusCode": "S"},
        "dma_result": {"DO_ERR_CODE": "HXI001", "DO_BILL_YM": "202701"},
        "dlt_chrtList": [{"DO_CHRT_REQ_YM": "202612"}],
    }

    bill = parse_bill(
        payload,
        requested_month="202612",
    )

    assert bill.bill_month == "202612"
    assert bill.response_bill_month == "202701"


@pytest.mark.parametrize(
    "payload",
    [
        {"rsMsg": {"statusCode": "S"}, "DO_BILL_YM": "202608"},
        {"rsMsg": {"statusCode": "S"}, "dma_result": {}, "BILL_YM": "202608"},
        {
            "rsMsg": {"statusCode": "S"},
            "dma_result": {"DO_BILL_YM": "202608"},
            "history": [{"BILL_YM": "202608"}],
        },
        {
            "rsMsg": {"statusCode": "S"},
            "dma_result": {"DO_BILL_YM": "202608"},
            "dlt_chrtList": [{"BILL_YM": "202608"}],
        },
    ],
)
def test_parse_bill_rejects_generic_only_payload_shapes(payload: dict[str, object]) -> None:
    with pytest.raises(KepcoOnProtocolError):
        parse_bill(payload, None)


def test_parse_bill_empty_values_are_none_and_comma_numbers_work() -> None:
    bill = parse_bill(
        {
            "rsMsg": {"statusCode": "S"},
            "dma_result": {
                "DO_ERR_CODE": "HXI001",
                "DO_BILL_YM": "202608",
                "DO_KWH": "",
                "DO_PRE_REQ_BILL": "96,330",
            },
        },
        None,
    )

    assert bill.usage_kwh is None
    assert bill.amount_krw == 96330


def test_coordinator_data_does_not_expose_raw_payload_field() -> None:
    assert "raw" not in KepcoCoordinatorData.__dataclass_fields__
