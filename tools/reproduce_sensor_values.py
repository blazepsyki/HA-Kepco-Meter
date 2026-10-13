"""Compare response field presence with production sensor values."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, fields
from getpass import getpass
from io import TextIOWrapper
from pathlib import Path
from typing import Any, cast

from aiohttp import ClientSession, CookieJar
from custom_components.kepco_on.api import KepcoOnClient
from custom_components.kepco_on.auth import KepcoOnAuth
from custom_components.kepco_on.const import (
    ENDPOINT_HOUSE_BILL_DETAIL,
    ENDPOINT_MAIN_CHART,
    ENDPOINT_POWER_PLANNER,
    VERSION,
)
from custom_components.kepco_on.coordinator import KepcoOnDataUpdateCoordinator
from custom_components.kepco_on.models import (
    KepcoAccountSession,
    KepcoBill,
    KepcoCoordinatorData,
    KepcoCustomer,
    customer_location_name,
)
from custom_components.kepco_on.parser import parse_power_planner_return_code
from custom_components.kepco_on.sensor import SENSOR_DESCRIPTIONS, KepcoOnSensor
from homeassistant.const import __version__ as HA_VERSION

ROOT = Path(__file__).resolve().parents[1]


class MemorySessionStore:
    """Keep authentication state in memory for this process only."""

    def __init__(self) -> None:
        self.session: KepcoAccountSession | None = None

    async def async_load(self) -> KepcoAccountSession | None:
        return self.session

    async def async_save(self, session: KepcoAccountSession) -> None:
        self.session = session

    async def async_clear(self) -> None:
        self.session = None


def value_shape(value: object) -> str:
    """Describe a value without retaining its contents."""
    if value is None:
        return "null"
    if isinstance(value, str):
        return "empty" if not value.strip() or value.strip().lower() == "null" else "text"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, list | tuple):
        return "array"
    return "object"


def response_summary(path: str, payload: dict[str, object]) -> dict[str, object]:
    """Allowlist response metadata; never include raw requests or values."""
    summary: dict[str, object] = {"endpoint": path}
    if path == ENDPOINT_MAIN_CHART:
        rows = payload.get("dlt_chart")
        summary["chart_shape"] = value_shape(rows)
        if isinstance(rows, list):
            summary["rows"] = [
                {
                    key: value_shape(row[key]) if key in row else "missing"
                    for key in ("jojYmFilter", "kwh", "afterMny", "gigan")
                }
                if isinstance(row, dict)
                else {"row_shape": value_shape(row)}
                for row in rows
            ]
    elif path == ENDPOINT_HOUSE_BILL_DETAIL:
        result = payload.get("dma_charge")
        summary["detail_shape"] = value_shape(result)
        if isinstance(result, dict):
            summary["fields"] = {
                key: value_shape(result[key]) if key in result else "missing"
                for key in ("whmMtrAlw", "bfrMtrAlw", "gumchmDd", "kwh", "befKwh", "lastYearKwh")
            }
        summary["charge_list_shape"] = value_shape(payload.get("dlt_mny"))
    elif path == ENDPOINT_POWER_PLANNER:
        result = payload.get("dma_powerPlanner")
        summary["planner_shape"] = value_shape(result)
        if isinstance(result, dict):
            summary["fields"] = {
                key: value_shape(result[key]) if key in result else "missing"
                for key in ("F_AP_QT", "PREDICT_TOT")
            }
        try:
            summary["return_code"] = parse_power_planner_return_code(payload)
        except Exception:
            summary["return_code_shape"] = "invalid"
    return summary


class ObservedAuth:
    """Delegate authentication while retaining only allowlisted response metadata."""

    def __init__(self, auth: KepcoOnAuth) -> None:
        self.auth = auth
        self.responses: list[dict[str, object]] = []

    async def async_protected_request(
        self,
        path: str,
        payload: dict[str, object] | None,
        *,
        submission_id: str | None = None,
    ) -> dict[str, object]:
        try:
            result = await self.auth.async_protected_request(
                path, payload, submission_id=submission_id
            )
        except Exception as error:
            self.responses.append({"endpoint": path, "error_type": type(error).__name__})
            raise
        self.responses.append(response_summary(path, result))
        return result

    def account_uid_hash(self) -> str:
        return self.auth.account_uid_hash()


@dataclass(slots=True)
class SnapshotCoordinator:
    """Expose only the coordinator state read by production sensor properties."""

    data: KepcoCoordinatorData
    last_update_success: bool = True


def sensor_report(
    customer: KepcoCustomer, bill: KepcoBill, responses: list[dict[str, object]], *, mode: str
) -> dict[str, object]:
    """Evaluate production entities without claiming HA state publication."""
    coordinator = SnapshotCoordinator(
        KepcoCoordinatorData(
            customers=(customer,), bills_by_customer_key={customer.stable_key: bill}
        )
    )
    translations = json.loads(
        (ROOT / "custom_components/kepco_on/translations/ko.json").read_text(encoding="utf-8")
    )["entity"]["sensor"]
    sensors: list[dict[str, object]] = []
    for description in SENSOR_DESCRIPTIONS:
        entity = KepcoOnSensor(
            cast("KepcoOnDataUpdateCoordinator", coordinator), customer, description, {}
        )
        value = entity.native_value
        sensors.append(
            {
                "key": description.key,
                "name": translations.get(description.translation_key, {}).get(
                    "name", description.key
                ),
                "history_month_offset": description.history_month_offset,
                "available": entity.available,
                "value_present": value is not None,
                "is_zero": isinstance(value, int | float) and value == 0,
                "expected_state": "value" if value is not None else "unknown",
                "planner_diagnostics": entity.extra_state_attributes
                if description.key in ("current_period_usage", "predicted_period_usage")
                else {},
            }
        )
    return {
        "mode": mode,
        "integration_version": VERSION,
        "home_assistant_library_version": HA_VERSION,
        "contract_profile": "house_direct" if customer.is_house else "apartment",
        "ha_state_publication_tested": False,
        "responses": responses,
        "parsed_fields": {
            field.name: getattr(bill, field.name) is not None
            for field in fields(bill)
            if field.name not in {"charge", "history"}
        },
        "parsed_charge_fields": {
            field.name: getattr(bill.charge, field.name) is not None
            for field in fields(bill.charge)
        },
        "history_point_count": len(bill.history),
        "house_detail_status": bill.house_detail_status,
        "sensors": sensors,
    }


class DemoAuth:
    """Supply synthetic house responses to the unchanged business client."""

    def __init__(self) -> None:
        self.responses: list[dict[str, object]] = []

    async def async_protected_request(
        self,
        path: str,
        payload: dict[str, object] | None,
        *,
        submission_id: str | None = None,
    ) -> dict[str, object]:
        del payload, submission_id
        if path == ENDPOINT_MAIN_CHART:
            result: dict[str, object] = {
                "dlt_chart": [
                    {
                        "jojYmFilter": f"2026{month:02d}",
                        "kwh": "100",
                        "afterMny": "10000",
                        "gigan": "2026.08.01-2026.08.31" if month == 9 else "",
                    }
                    for month in range(1, 10)
                ]
            }
            rows = cast("list[dict[str, object]]", result["dlt_chart"])
            rows[:0] = [
                {
                    "jojYmFilter": f"{year}{month:02d}",
                    "kwh": "100",
                    "afterMny": "10000",
                    "gigan": "",
                }
                for year, month in (
                    [(2024, month) for month in range(10, 13)]
                    + [(2025, month) for month in range(1, 13)]
                )
            ]
        elif path == ENDPOINT_HOUSE_BILL_DETAIL:
            result = {
                "rsMsg": {"statusCode": "S"},
                "dma_charge": {
                    "yy": "2026",
                    "mm": "09",
                    "whmMtrAlw": "1200.00",
                    "bfrMtrAlw": "1100.00",
                    "gumchmDd": "1",
                    "kwh": "100",
                },
                "dlt_mny": [
                    {"mnyTitle": "기본요금", "mnyMny": "1000"},
                    {"mnyTitle": "전력량요금", "mnyMny": "6000"},
                    {"mnyTitle": "기후환경요금", "mnyMny": "1000"},
                    {"mnyTitle": "연료비조정요금", "mnyMny": "1000"},
                    {"mnyTitle": "출산가구할인", "mnyMny": "-200"},
                    {"mnyTitle": "전기요금계", "mnyMny": "8800"},
                    {"mnyTitle": "부가가치세", "mnyMny": "880"},
                    {"mnyTitle": "전력산업기반기금", "mnyMny": "320"},
                    {"mnyTitle": "원단위절사", "mnyMny": "0"},
                    {"mnyTitle": "청구금액", "mnyMny": "10000"},
                ],
            }
        elif path == ENDPOINT_POWER_PLANNER:
            result = {"dma_powerPlanner": {"RETURN_CD": "00", "F_AP_QT": None, "PREDICT_TOT": 0}}
        else:
            raise ValueError("Unexpected demo endpoint")
        self.responses.append(response_summary(path, result))
        return result


async def demo_report() -> dict[str, object]:
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
    auth = DemoAuth()
    bill = await KepcoOnClient(cast("Any", auth)).async_get_bill(customer)
    return sensor_report(customer, bill, auth.responses, mode="synthetic_house")


async def live_report() -> dict[str, object]:
    if not sys.stdin.isatty():
        raise ValueError("Live mode requires an interactive terminal")
    username = (await asyncio.to_thread(input, "한전ON 아이디: ")).strip()
    password = await asyncio.to_thread(getpass, "한전ON 비밀번호(화면에 표시되지 않습니다): ")
    if not username or not password:
        raise ValueError("Credentials are empty")
    stage = "login"
    async with ClientSession(cookie_jar=CookieJar()) as session:
        auth = KepcoOnAuth(session, store=MemorySessionStore())
        observed = ObservedAuth(auth)
        client = KepcoOnClient(cast("Any", observed))
        try:
            await auth.async_login(username, password)
            password = ""
            stage = "account_type"
            await client.async_get_account_type()
            stage = "customers"
            customers = await client.async_get_customers()
            for index, customer in enumerate(customers, start=1):
                print(f"{index}. {customer.contract_method} / {customer_location_name(customer)}")
            if not customers:
                raise ValueError("No supported customers")
            selection = int(await asyncio.to_thread(input, "조회할 고객 번호(위 목록의 순번): "))
            if not 1 <= selection <= len(customers):
                raise ValueError("Customer selection is out of range")
            stage = "bill"
            customer = customers[selection - 1]
            bill = await client.async_get_bill(customer)
            return sensor_report(customer, bill, observed.responses, mode="live")
        except Exception as error:
            return {
                "mode": "live",
                "failed_stage": stage,
                "error_type": type(error).__name__,
                "responses": observed.responses,
            }
        finally:
            password = ""


def main() -> int:
    if isinstance(sys.stdout, TextIOWrapper) and not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="현재 코드로 센서 값 누락을 재현합니다.")
    parser.add_argument("--demo", action="store_true", help="실제 접속 없이 주택용 합성 응답 사용")
    parser.add_argument("--output", type=Path, help="값과 인증 정보를 제외한 진단 JSON 저장 경로")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        report = asyncio.run(demo_report() if args.demo else live_report())
    except EOFError, KeyboardInterrupt:
        print("입력을 취소했습니다.")
        return 1
    except Exception as error:
        print(f"재현 도구를 실행하지 못했습니다: {type(error).__name__}")
        return 1
    if args.output is not None:
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"진단 결과 저장: {args.output}")
    for sensor in cast("list[dict[str, object]]", report.get("sensors", [])):
        state = "값 있음" if sensor["value_present"] else "알 수 없음"
        print(f"{sensor['key']}: {state}")
    if "failed_stage" in report:
        print(f"조회 실패 단계: {report['failed_stage']} / {report['error_type']}")
        return 1
    print("실제 HA 상태 기록과 HACS 설치 과정은 이 도구의 검증 범위에 포함되지 않습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
