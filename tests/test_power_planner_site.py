"""Synthetic regressions for the optional Power Planner site integration."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiohttp import ClientSession, CookieJar
from aresponses import Response, ResponsesMockServer
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from custom_components.kepco_on.const import (
    CONF_USERNAME,
    OPT_ENABLE_POWER_PLANNER,
    OPT_POWER_PLANNER_CUSTOMER_ID,
)
from custom_components.kepco_on.exceptions import (
    KepcoOnAuthError,
    KepcoOnConnectionError,
    KepcoOnNoCustomersError,
    KepcoOnProtocolError,
    KepcoOnRateLimitError,
    KepcoOnSessionExpired,
    KepcoOnUnsupportedAccount,
)
from custom_components.kepco_on.models import PowerPlannerData, PowerPlannerSnapshot
from custom_components.kepco_on.power_planner import (
    KOREA,
    ORIGIN,
    PowerPlannerClient,
    PowerPlannerSessionStore,
    _object,
    decode_cookie,
    parse_daily,
    planner_date,
    planner_quantity,
)
from custom_components.kepco_on.power_planner_coordinator import PowerPlannerCoordinator
from custom_components.kepco_on.sensor import PLANNER_DESCRIPTIONS, PowerPlannerSensor
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from multidict import CIMultiDict

from .test_api_house import _house_customer
from .test_planner_state_publication import publish
from .test_sensor import FakeCoordinator

NOW = datetime(2026, 8, 16, 12, tzinfo=KOREA)


def smart() -> dict[str, object]:
    return cast(
        "dict[str, object]",
        json.loads(
            (Path(__file__).parent / "fixtures/planner_smartview.json").read_text(encoding="utf-8")
        ),
    )


def chart(valid: int = 24) -> list[dict[str, object]]:
    return [
        {
            "MR_HHMI": f"{index:02d}시",
            "F_AP_QT": "0" if index == 0 else "1.25",
            "NO_DATA_YN": "N" if index < valid else "Y",
        }
        for index in range(24)
    ]


class MemoryStore:
    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id
        self.saved: list[str] = []
        self.clears = 0

    async def async_load(self) -> str | None:
        return self.session_id

    async def async_save(self, session_id: str) -> None:
        self.session_id = session_id
        self.saved.append(session_id)

    async def async_clear(self) -> None:
        self.session_id = None
        self.clears += 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("null", None),
        (0, 0),
        ("0", 0),
        ("1,234.50", 1234.5),
        (1.25, 1.25),
    ],
)
def test_quantity_preserves_zero_and_explicit_units(value: object, expected: float | None) -> None:
    assert planner_quantity(value) == expected


@pytest.mark.parametrize(
    "value", [True, False, -1, "-1", "NaN", "Infinity", "1,2", {}, [], "1e300", "9" * 400]
)
def test_invalid_quantities_are_not_published(value: object) -> None:
    with pytest.raises(KepcoOnProtocolError):
        planner_quantity(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("TEST%2BSESSION%2F", "TEST+SESSION/"), ("TEST+SESSION", "TEST+SESSION")],
)
def test_cookie_decode_preserves_plus(value: str, expected: str) -> None:
    assert decode_cookie(value) == expected


@pytest.mark.parametrize("value", ["bad%GG", "%FF", "bad%"])
def test_cookie_decode_rejects_invalid_encoding(value: str) -> None:
    with pytest.raises(KepcoOnProtocolError):
        decode_cookie(value)


@pytest.mark.parametrize("value", [None, 20260816, "20260816", "2026-02-30", "private_value"])
def test_invalid_periods_are_rejected(value: object) -> None:
    with pytest.raises(KepcoOnProtocolError):
        planner_date(value)


def test_dates_are_normalized_without_billing_fallback() -> None:
    assert planner_date("2026.08.16") == date(2026, 8, 16)
    assert planner_date("2026-08-16") == date(2026, 8, 16)


@pytest.mark.parametrize(
    ("valid", "completed", "status"),
    [(0, False, "no_data"), (16, False, "partial"), (16, True, "incomplete"), (24, True, "ok")],
)
def test_daily_missing_markers_override_numeric_placeholders(
    valid: int, completed: bool, status: str
) -> None:
    rows = chart(valid)
    value, recent, count, result = parse_daily([{"F_AP_QT": "123.45"}], rows, completed=completed)
    assert result == status
    assert count == valid
    assert (value is None) == (valid == 0 or (completed and valid != 24))
    assert (recent is None) == (valid == 0)


@pytest.mark.parametrize(
    "case",
    [
        "short",
        "bad_total",
        "not_list",
        "bad_row",
        "duplicate",
        "label",
        "hour",
        "marker",
        "missing_numeric",
    ],
)
def test_malformed_interval_contract_is_rejected(case: str) -> None:
    rows: Any = chart()
    totals: Any = [{"F_AP_QT": "123"}]
    if case == "short":
        rows.pop()
    elif case == "bad_total":
        totals = []
    elif case == "not_list":
        rows = {}
    elif case == "bad_row":
        rows[0] = None
    elif case == "duplicate":
        rows[1]["MR_HHMI"] = "00시"
    elif case == "label":
        rows[0]["MR_HHMI"] = None
    elif case == "hour":
        rows[0]["MR_HHMI"] = "25시"
    elif case == "marker":
        rows[0]["NO_DATA_YN"] = "other"
    else:
        rows[0]["F_AP_QT"] = "NaN"
    with pytest.raises(KepcoOnProtocolError):
        parse_daily(totals, rows, completed=False)


class DataClient(PowerPlannerClient):
    """Replace only the transport; use the production parser and cache."""

    def __init__(self, session: ClientSession, *, current: dict[str, object] | None = None) -> None:
        super().__init__(session, clock=lambda: NOW)
        self.current = current if current is not None else smart()
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.daily_error = False

    async def _json(self, path: str, payload: dict[str, object]) -> object:
        self.calls.append((path, payload))
        if path == "/rm/getRM0201.do":
            return self.current
        if path == "/auth/usercustno_list.do":
            return [{"CUSTNO": "TEST_SI_CUST_001"}]
        if path == "/rm/rm0101_contract_info.do":
            return {"CUSTNO": "TEST_SI_CUST_001", "SESSION_MR_CYCLE_MIN": 60}
        if path == "/auth/refreshToken.do":
            return {}
        if self.daily_error:
            raise KepcoOnProtocolError("synthetic")
        if path.endswith("_list.do"):
            return [{"F_AP_QT": "12.5"}]
        return chart(16 if payload.get("SELECT_DT") == "2026-08-16" else 24)


async def test_snapshot_separates_quantities_and_caches_only_complete_previous_day() -> None:
    async with ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session:
        client = DataClient(session)
        first = await client._read_snapshot()
        second = await client._read_snapshot()
        assert first == second
        assert first.current_usage_kwh == 123.45
        assert first.predicted_usage_kwh == 234.56
        assert first.current_amount_krw == 12345
        assert first.predicted_amount_krw == 23456
        assert first.today_usage_kwh == 12.5
        assert first.today_valid_intervals == 16
        assert first.field_statuses["today_usage_kwh"] == "partial"
        assert sum(payload.get("SELECT_DT") == "2026-08-15" for _, payload in client.calls) == 2


@pytest.mark.parametrize(
    "field", ["F_AP_QT", "PREDICT_TOT", "TOTAL_CHARGE", "PREDICT_TOTAL_CHARGE"]
)
async def test_invalid_optional_field_does_not_erase_other_snapshot_fields(field: str) -> None:
    data = smart()
    data[field] = "NaN"
    async with ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session:
        snapshot = await DataClient(session, current=data)._read_snapshot()
        assert list(snapshot.field_statuses.values()).count("invalid_response") == 1
        assert snapshot.today_usage_kwh == 12.5


async def test_optional_daily_schema_failure_keeps_smartview() -> None:
    async with ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session:
        client = DataClient(session)
        client.daily_error = True
        snapshot = await client._read_snapshot()
        assert snapshot.current_usage_kwh == 123.45
        assert snapshot.today_usage_kwh is None
        assert snapshot.field_statuses["yesterday_usage_kwh"] == "invalid_response"


@pytest.mark.parametrize(
    "changes", [{"START_DT": "2026.09.01"}, {"END_DT": "2026.08.15"}, {"SELECT_DT": "2026.10.01"}]
)
async def test_current_snapshot_rejects_wrong_period(changes: dict[str, object]) -> None:
    data = smart()
    data.update(changes)
    async with ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session:
        with pytest.raises(KepcoOnProtocolError):
            await DataClient(session, current=data)._read_snapshot()


async def test_selected_household_must_match_queried_contract() -> None:
    async with ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session:
        client = DataClient(session)
        with pytest.raises(KepcoOnNoCustomersError):
            await client.async_validate_customers({"TEST_BUILDING_ONLY"})
        await client.async_validate_customers({"TEST_SI_CUST_001"})
        assert client.main_customer_number == "TEST_SI_CUST_001"


async def test_client_requires_unquoted_cookie_jar() -> None:
    async with ClientSession() as session:
        with pytest.raises(ValueError, match="CookieJar"):
            PowerPlannerClient(session)


async def test_login_uses_decoded_prefix_and_encrypted_fields_only() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    modulus = format(private.public_key().public_numbers().n, "x")
    headers = CIMultiDict(
        [
            ("Set-Cookie", f"cookieRsa={modulus}; Path=/"),
            ("Set-Cookie", "cookieSsId=TEST%2BSESSION; Path=/"),
            ("Set-Cookie", "JSESSIONID=TEST_SESSION; Path=/"),
        ]
    )

    async def checked(request: Any) -> Response:
        payload = await request.json()
        for field, expected in [("USER_ID", "TEST_USER"), ("USER_PWD", "TEST_PASSWORD")]:
            prefix, hexadecimal = payload[field].rsplit("_", 1)
            assert prefix == "TEST+SESSION"
            assert (
                private.decrypt(
                    bytes.fromhex(hexadecimal).rjust(256, b"\0"), padding.PKCS1v15()
                ).decode()
                == expected
            )
        return Response(
            text='{"result":"success","USER_SSO_YN":"Y"}', content_type="application/json"
        )

    store = MemoryStore()
    async with (
        ResponsesMockServer() as server,
        ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session,
    ):
        server.add(
            "pp.kepco.co.kr",
            "/intro.do",
            "get",
            Response(text='<input id="RSAExponent" value="10001">', headers=headers),
        )
        server.add("pp.kepco.co.kr", "/intro/chkUser.do", "post", checked)
        server.add(
            "pp.kepco.co.kr",
            "/login",
            "post",
            Response(status=302, headers={"Location": "/rm/rm0201.do"}),
        )
        server.add("pp.kepco.co.kr", "/rm/rm0201.do", "get", Response(text="로그아웃 스마트뷰"))
        client = PowerPlannerClient(session, store)
        await client.async_login("TEST_USER", "TEST_PASSWORD")
        assert store.saved == ["TEST_SESSION"]


@pytest.mark.parametrize("payload", ["[]", "null", "{SECRET_CANARY", "<html>not json</html>"])
async def test_transport_rejects_bad_json_without_leaking_body(payload: str) -> None:
    async with (
        ResponsesMockServer() as server,
        ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session,
    ):
        server.add("pp.kepco.co.kr", "/rm/getRM0201.do", "post", Response(text=payload))
        client = PowerPlannerClient(session)
        if payload in {"[]", "null"}:
            with pytest.raises(KepcoOnProtocolError):
                _object(await client._json("/rm/getRM0201.do", {}))
        else:
            with pytest.raises(KepcoOnProtocolError) as caught:
                await client._json("/rm/getRM0201.do", {})
            assert caught.value.__context__ is None
            assert "SECRET_CANARY" not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, KepcoOnSessionExpired),
        (403, KepcoOnSessionExpired),
        (429, KepcoOnRateLimitError),
        (404, KepcoOnConnectionError),
        (302, KepcoOnSessionExpired),
    ],
)
async def test_transport_maps_provider_failures(status: int, error: type[Exception]) -> None:
    async with (
        ResponsesMockServer() as server,
        ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session,
    ):
        server.add("pp.kepco.co.kr", "/auth/usercustno_list.do", "post", Response(status=status))
        with pytest.raises(error):
            await PowerPlannerClient(session)._json("/auth/usercustno_list.do", {})


@pytest.mark.parametrize(
    "location",
    [
        None,
        "https://other.example/rm/rm0201.do",
        "/intro.do?status=LOGIN_FAILED",
        "/rm/rm0201.do?token=SECRET",
    ],
)
async def test_login_redirect_does_not_forward_credentials(location: str | None) -> None:
    async with (
        ResponsesMockServer() as server,
        ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session,
    ):
        server.add(
            "pp.kepco.co.kr",
            "/login",
            "post",
            Response(status=302, headers={} if location is None else {"Location": location}),
        )
        with pytest.raises((KepcoOnProtocolError, KepcoOnAuthError)):
            await PowerPlannerClient(session)._request("POST", "/login", form={"USER_ID": "SECRET"})


async def test_read_retry_is_bounded_and_paths_are_fixed() -> None:
    sleeps = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with (
        ResponsesMockServer() as server,
        ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session,
    ):
        for status in [503, 502, 200]:
            server.add(
                "pp.kepco.co.kr",
                "/auth/usercustno_list.do",
                "post",
                Response(text="[]", status=status),
            )
        client = PowerPlannerClient(session, sleep=sleep)
        assert await client._json("/auth/usercustno_list.do", {}) == []
        assert sleeps == [1.0, 2.0]
        with pytest.raises(KepcoOnProtocolError):
            await client._request("POST", "/unknown")


async def test_restored_session_is_validated_before_use() -> None:
    async with (
        ResponsesMockServer() as server,
        ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session,
    ):
        server.add("pp.kepco.co.kr", "/rm/rm0201.do", "get", Response(text="로그아웃 스마트뷰"))
        client = PowerPlannerClient(session, MemoryStore("TEST_SESSION"))
        await client._ensure("TEST_USER", None)
        assert client._authenticated
        assert session.cookie_jar.filter_cookies(ORIGIN)["JSESSIONID"].value == "TEST_SESSION"


async def test_expired_session_without_saved_password_requires_only_optional_auth() -> None:
    store = MemoryStore("TEST_SESSION")
    async with (
        ResponsesMockServer() as server,
        ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session,
    ):
        server.add("pp.kepco.co.kr", "/rm/rm0201.do", "get", Response(status=302))
        with pytest.raises(KepcoOnAuthError):
            await PowerPlannerClient(session, store)._ensure("TEST_USER", None)
        assert store.clears == 1


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (KepcoOnAuthError("PRIVATE"), "auth_required"),
        (KepcoOnConnectionError("PRIVATE"), "connection_error"),
        (KepcoOnRateLimitError("PRIVATE"), "rate_limited"),
        (KepcoOnProtocolError("PRIVATE"), "invalid_response"),
        (KepcoOnUnsupportedAccount("PRIVATE"), "unsupported_profile"),
        (KepcoOnNoCustomersError("PRIVATE"), "unsupported_customer"),
        (OSError("PRIVATE"), "storage_error"),
    ],
)
async def test_optional_coordinator_failure_is_status_not_entry_failure(
    hass: HomeAssistant, error: Exception, status: str
) -> None:
    class Client:
        async def async_poll(self, *args: object, **kwargs: object) -> None:
            raise error

    entry = SimpleNamespace(
        entry_id="planner",
        data={CONF_USERNAME: "PRIVATE_USER"},
        async_on_unload=lambda callback: None,
    )
    coordinator = PowerPlannerCoordinator(
        hass, cast("Any", entry), cast("Any", Client()), (_house_customer(),), None
    )
    result = await coordinator._async_update_data()
    assert result.snapshots["house-key"].status == status
    assert "PRIVATE" not in str(result)


async def test_keepalive_does_not_refetch_quantities_each_tick(hass: HomeAssistant) -> None:
    class Client:
        main_customer_number = "TEST_SI_CUST_001"

        def __init__(self) -> None:
            self.calls: list[bool] = []

        async def async_poll(
            self, *args: object, fetch: bool, **kwargs: object
        ) -> PowerPlannerSnapshot:
            self.calls.append(fetch)
            return PowerPlannerSnapshot(status="ok", current_usage_kwh=0)

    client = Client()
    entry = SimpleNamespace(
        entry_id="planner",
        data={CONF_USERNAME: "PRIVATE_USER"},
        async_on_unload=lambda callback: None,
    )
    coordinator = PowerPlannerCoordinator(
        hass, cast("Any", entry), cast("Any", client), (_house_customer(),), None
    )
    first = await coordinator._async_update_data()
    second = await coordinator._async_update_data()
    assert first == second
    assert client.calls == [True, False]


async def test_planner_sensors_publish_same_ids_and_independent_status(hass: HomeAssistant) -> None:
    customer = _house_customer()
    snapshot = PowerPlannerSnapshot(
        status="ok",
        current_usage_kwh=0,
        predicted_usage_kwh=234.56,
        current_amount_krw=12345,
        predicted_amount_krw=23456,
        today_usage_kwh=12.5,
        yesterday_usage_kwh=24,
        recent_interval_usage_kwh=1.25,
        calendar_date=date(2026, 8, 16),
        period_start=date(2026, 8, 9),
        period_end=date(2026, 9, 8),
        interval_minutes=60,
    )
    coordinator = cast(
        "Any", FakeCoordinator(cast("Any", PowerPlannerData({customer.stable_key: snapshot})))
    )
    entities = [
        PowerPlannerSensor(coordinator, customer, description)
        for description in PLANNER_DESCRIPTIONS
        if description.entity_registry_enabled_default
    ]
    sensors = await publish(
        hass,
        cast("list[Any]", entities),
        tuple(entity.entity_description.key for entity in entities),
    )
    for entity in sensors.values():
        state = hass.states.get(entity.entity_id)
        assert state is not None
        assert state.attributes["data_source"] == "kepco_power_planner_site"
        assert state.attributes["value_divisor"] == 1
        assert entity.unique_id == f"house-key_{entity.entity_description.key}"
        assert "TEST_SI_CUST_001" not in str(state.attributes)
    current_state = hass.states.get(sensors["current_period_usage"].entity_id)
    assert current_state is not None
    assert float(current_state.state) == 0
    coordinator.data = PowerPlannerData(
        {customer.stable_key: PowerPlannerSnapshot(status="auth_required")}
    )
    for entity in entities:
        entity.async_write_ha_state()
        state = hass.states.get(entity.entity_id)
        assert state is not None
        assert state.state == "unknown"
        assert state.attributes["data_status"] == "auth_required"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"session_id": "TEST_SESSION"},
        {"session_id": "bad\r\nheader"},
        [],
        {"session_id": 123},
    ],
)
async def test_private_store_validates_cookie_and_uses_separate_namespace(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    import custom_components.kepco_on.power_planner as module

    class Storage:
        def __init__(self, *args: Any) -> None:
            assert args[2] == "kepco_on.power_planner.entry"

        async def async_load(self) -> Any:
            return payload

        async def async_save(self, value: Any) -> None:
            assert value == {"session_id": "TEST_SESSION"}

        async def async_remove(self) -> None:
            return None

    monkeypatch.setattr(module, "Store", Storage)
    store = PowerPlannerSessionStore(hass, "entry")
    if payload is None or payload == {"session_id": "TEST_SESSION"}:
        assert await store.async_load() == (None if payload is None else "TEST_SESSION")
    else:
        with pytest.raises(KepcoOnProtocolError):
            await store.async_load()
    await store.async_save("TEST_SESSION")
    await store.async_clear()
    with pytest.raises(KepcoOnProtocolError):
        await store.async_save("bad\r\nheader")


async def test_empty_daily_aggregate_does_not_create_zero() -> None:
    rows = chart()
    rows[0]["F_AP_QT"] = None
    quantity, _, count, status = parse_daily([{"F_AP_QT": None}], rows, completed=False)
    assert quantity is None
    assert count == 23
    assert status == "no_data"


@pytest.mark.parametrize(
    "values",
    [
        {"CUSTNO": "UNKNOWN", "SESSION_MR_CYCLE_MIN": 60},
        {"CUSTNO": "TEST_SI_CUST_001", "SESSION_MR_CYCLE_MIN": 15},
    ],
)
async def test_identity_and_unverified_meter_profile_are_rejected(
    values: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session:
        client = PowerPlannerClient(session)

        async def response(path: str, payload: dict[str, object]) -> object:
            return (
                [{"CUSTNO": "TEST_SI_CUST_001"}] if path == "/auth/usercustno_list.do" else values
            )

        monkeypatch.setattr(client, "_json", response)
        with pytest.raises((KepcoOnProtocolError, KepcoOnUnsupportedAccount)):
            await client.async_validate_customers({"TEST_SI_CUST_001"})


async def test_poll_serializes_and_does_not_refetch_on_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session:
        client = DataClient(session)
        login_calls = []

        async def login(username: str, password: str) -> None:
            login_calls.append((username, password))
            client._authenticated = True
            session.cookie_jar.update_cookies({"JSESSIONID": "TEST_SESSION"}, response_url=ORIGIN)

        monkeypatch.setattr(client, "async_login", login)
        first = await client.async_poll("TEST_USER", "TEST_PASSWORD", {"TEST_SI_CUST_001"})
        count = len(client.calls)
        second = await client.async_poll("TEST_USER", None, {"TEST_SI_CUST_001"}, fetch=False)
        assert first == second
        assert len(client.calls) == count + 1
        assert len(login_calls) == 1


@pytest.mark.parametrize(
    ("password", "failures", "expected_logins"),
    [(None, 1, 0), ("TEST_PASSWORD", 1, 1), ("TEST_PASSWORD", 2, 1)],
)
async def test_expiration_replays_at_most_once(
    password: str | None, failures: int, expected_logins: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    async with ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session:
        client = PowerPlannerClient(session, store)
        client._authenticated = True
        login_calls = []

        async def login(username: str, password: str) -> None:
            login_calls.append(username)
            client._authenticated = True
            session.cookie_jar.update_cookies({"JSESSIONID": "TEST_SESSION"}, response_url=ORIGIN)

        calls = 0

        async def response(path: str, payload: dict[str, object]) -> object:
            nonlocal calls
            calls += 1
            if calls <= failures:
                raise KepcoOnSessionExpired("PRIVATE")
            return {}

        monkeypatch.setattr(client, "async_login", login)

        async def validate(customers: set[str]) -> None:
            return None

        async def snapshot() -> PowerPlannerSnapshot:
            return PowerPlannerSnapshot(status="ok")

        monkeypatch.setattr(client, "_json", response)
        monkeypatch.setattr(client, "async_validate_customers", validate)
        monkeypatch.setattr(client, "_read_snapshot", snapshot)
        if password is None or failures == 2:
            with pytest.raises(KepcoOnAuthError):
                await client.async_poll("TEST_USER", password, set(), fetch=False)
        else:
            result = await client.async_poll("TEST_USER", password, set(), fetch=False)
            assert result is not None
            assert result.status == "ok"
        assert len(login_calls) == expected_logins


async def test_corrupt_restore_can_recover_with_saved_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CorruptStore(MemoryStore):
        async def async_load(self) -> str | None:
            raise KepcoOnProtocolError("PRIVATE")

    store = CorruptStore()
    async with ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session:
        client = PowerPlannerClient(session, store)

        async def login(username: str, password: str) -> None:
            client._authenticated = True

        monkeypatch.setattr(client, "async_login", login)
        await client._ensure("TEST_USER", "TEST_PASSWORD")
        assert client._authenticated
        assert store.clears == 1


async def test_missing_authentication_cookie_is_protocol_error() -> None:
    async with ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session:
        with pytest.raises(KepcoOnProtocolError):
            await PowerPlannerClient(session)._persist()


@pytest.mark.parametrize("case", ["credentials", "rsa", "account", "redirect", "landing"])
async def test_login_failures_are_safe_and_bounded(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async with ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session:
        client = PowerPlannerClient(session)
        session.cookie_jar.update_cookies({"JSESSIONID": "TEST_SESSION"}, response_url=ORIGIN)

        async def request(method: str, path: str, **kwargs: object) -> tuple[bytes, str | None]:
            if path == "/intro.do":
                if case != "rsa":
                    session.cookie_jar.update_cookies(
                        {
                            "cookieSsId": "TEST%2BSESSION",
                            "cookieRsa": format(private.public_key().public_numbers().n, "x"),
                        },
                        response_url=ORIGIN,
                    )
                return b'<input id="RSAExponent" value="10001">', None
            if path == "/intro/chkUser.do":
                return (
                    b'{"result":"fail"}'
                    if case == "account"
                    else b'{"result":"success","USER_SSO_YN":"Y"}'
                ), None
            if path == "/login":
                return b"", None if case == "redirect" else "/rm/rm0201.do"
            return b"not authenticated", None

        monkeypatch.setattr(client, "_request", request)
        with pytest.raises((KepcoOnAuthError, KepcoOnProtocolError)) as caught:
            await client.async_login("" if case == "credentials" else "TEST_USER", "TEST_PASSWORD")
        assert "TEST_PASSWORD" not in str(caught.value)


async def test_network_retry_failure_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp import ClientConnectionError

    sleeps = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session:

        def request(*args: object, **kwargs: object) -> Any:
            raise ClientConnectionError("PRIVATE")

        monkeypatch.setattr(session, "request", request)
        with pytest.raises(KepcoOnConnectionError) as caught:
            await PowerPlannerClient(session, sleep=sleep)._request("GET", "/intro.do")
        assert sleeps == [1, 2]
        assert "PRIVATE" not in str(caught.value)


@pytest.fixture
def option_fakes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import custom_components.kepco_on.config_flow as module

    from .test_config_flow import RAW_HOUSE_SECRET

    state: dict[str, Any] = {
        "store": MemoryStore(),
        "error": None,
        "logins": [],
        "sessions": [],
        "mismatch": False,
    }

    def factory(hass: object, *, cookie_jar: CookieJar, **kwargs: object) -> ClientSession:
        session = ClientSession(cookie_jar=cookie_jar)
        state["sessions"].append(session)
        return session

    class Client:
        def __init__(self, session: ClientSession) -> None:
            self.session = session
            self.main_customer_number = "WRONG_CUSTOMER" if state["mismatch"] else RAW_HOUSE_SECRET

        async def async_login(self, username: str, password: str) -> None:
            state["logins"].append((username, password))
            if state["error"]:
                raise state["error"]
            self.session.cookie_jar.update_cookies(
                {"JSESSIONID": "TEST_SESSION"}, response_url=ORIGIN
            )

        async def async_validate_customers(self, customers: set[str]) -> None:
            if state["error"]:
                raise state["error"]

    monkeypatch.setattr(module, "async_create_clientsession", factory)
    monkeypatch.setattr(module, "PowerPlannerClient", Client)
    monkeypatch.setattr(module, "PowerPlannerSessionStore", lambda *args: state["store"])
    return state


@pytest.mark.parametrize("saved_password", [False, True])
async def test_options_enable_binds_one_customer_without_storing_password(
    option_fakes: dict[str, Any],
    saved_password: bool,
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnOptionsFlow

    from .test_config_flow import attach_fake_hass, make_entry

    entry = make_entry()
    if saved_password:
        entry.data[CONF_PASSWORD] = "TEST_PASSWORD"
    flow = KepcoOnOptionsFlow(cast("Any", entry))
    attach_fake_hass(flow)
    result = await flow.async_step_init({OPT_ENABLE_POWER_PLANNER: True})
    if not saved_password:
        assert result["step_id"] == "power_planner_auth"
        schema = result["data_schema"]
        assert schema is not None
        password_key = next(key for key in schema.schema if key.schema == CONF_PASSWORD)
        assert not callable(password_key.default)
        result = await flow.async_step_power_planner_auth({CONF_PASSWORD: "TEST_PASSWORD"})
    assert result["type"] == "create_entry"
    assert result["data"][OPT_POWER_PLANNER_CUSTOMER_ID] == "key-1"
    assert CONF_PASSWORD not in result["data"]
    assert option_fakes["store"].saved == ["TEST_SESSION"]
    assert all(session.closed for session in option_fakes["sessions"])


@pytest.mark.parametrize(
    "case",
    ["missing_password", "invalid_password", "mismatch", "storage_error", "bad_password_type"],
)
async def test_options_failure_does_not_enable_provider(
    case: str, option_fakes: dict[str, Any]
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnOptionsFlow

    from .test_config_flow import attach_fake_hass, make_entry

    entry = make_entry()
    flow = KepcoOnOptionsFlow(cast("Any", entry))
    attach_fake_hass(flow)
    inputs: dict[str, Any] = {OPT_ENABLE_POWER_PLANNER: True}
    if case != "missing_password":
        entry.data[CONF_PASSWORD] = "TEST_PASSWORD"
    if case == "invalid_password":
        option_fakes["error"] = KepcoOnAuthError("PRIVATE")
    if case == "mismatch":
        option_fakes["mismatch"] = True
    if case == "storage_error":
        option_fakes["error"] = OSError("PRIVATE")
    if case == "bad_password_type":
        entry.data[CONF_PASSWORD] = 123
    result = await flow.async_step_init(inputs)
    assert result["type"] == "form"
    if case in {"missing_password", "invalid_password", "bad_password_type"}:
        assert result["step_id"] == "power_planner_auth"
    if case == "invalid_password":
        result = await flow.async_step_power_planner_auth({CONF_PASSWORD: "TEST_PASSWORD"})
        assert result["errors"] == {"base": "invalid_auth"}
    assert OPT_ENABLE_POWER_PLANNER not in entry.options
    assert all(session.closed for session in option_fakes["sessions"])


async def test_options_restore_cookie_and_disable_clear_only_own_store(
    option_fakes: dict[str, Any],
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnOptionsFlow

    from .test_config_flow import attach_fake_hass, make_entry

    option_fakes["store"].session_id = "TEST_SESSION"
    entry = make_entry()
    flow = KepcoOnOptionsFlow(cast("Any", entry))
    attach_fake_hass(flow)
    result = await flow.async_step_init({OPT_ENABLE_POWER_PLANNER: True})
    assert result["type"] == "create_entry"
    assert option_fakes["logins"] == []
    entry.options = {OPT_ENABLE_POWER_PLANNER: True, OPT_POWER_PLANNER_CUSTOMER_ID: "key-1"}
    next_flow = KepcoOnOptionsFlow(cast("Any", entry))
    attach_fake_hass(next_flow)
    result = await next_flow.async_step_init({OPT_ENABLE_POWER_PLANNER: False})
    assert result["type"] == "create_entry"
    assert OPT_POWER_PLANNER_CUSTOMER_ID not in result["data"]
    assert option_fakes["store"].clears == 1


async def test_existing_enable_retains_binding_without_network_login(
    option_fakes: dict[str, Any],
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnOptionsFlow

    from .test_config_flow import attach_fake_hass, make_entry

    entry = make_entry()
    entry.options = {OPT_ENABLE_POWER_PLANNER: True, OPT_POWER_PLANNER_CUSTOMER_ID: "key-1"}
    flow = KepcoOnOptionsFlow(cast("Any", entry))
    attach_fake_hass(flow)
    result = await flow.async_step_init({})
    assert result["data"][OPT_POWER_PLANNER_CUSTOMER_ID] == "key-1"
    assert option_fakes["sessions"] == []
    assert flow._validate_options({OPT_ENABLE_POWER_PLANNER: "bad"})[1] == "unknown"


@pytest.mark.parametrize("case", ["empty_customers", "no_snapshot"])
async def test_coordinator_empty_states(hass: HomeAssistant, case: str) -> None:
    class Client:
        async def async_poll(self, *args: object, **kwargs: object) -> None:
            return None

    entry = SimpleNamespace(
        entry_id="planner", data={CONF_USERNAME: "PRIVATE"}, async_on_unload=lambda callback: None
    )
    coordinator = PowerPlannerCoordinator(
        hass,
        cast("Any", entry),
        cast("Any", Client()),
        () if case == "empty_customers" else (_house_customer(),),
        None,
    )
    data = await coordinator._async_update_data()
    if case == "no_snapshot":
        assert data.snapshots["house-key"].status == "no_data"
    else:
        assert not data.snapshots


async def test_login_navigation_parameter_is_discarded() -> None:
    async with (
        ResponsesMockServer() as server,
        ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session,
    ):
        server.add(
            "pp.kepco.co.kr",
            "/login",
            "post",
            Response(status=302, headers={"Location": "/rm/rm0201.do?menu_id=IGNORED_CANARY"}),
        )
        body, target = await PowerPlannerClient(session)._request("POST", "/login", form={})
        assert body == b""
        assert target == "/rm/rm0201.do"


@pytest.mark.parametrize("path", ["/login", "/intro/chkUser.do"])
async def test_auth_network_failure_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    from aiohttp import ClientConnectionError

    calls = []
    async with ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session:

        def failed(*args: object, **kwargs: object) -> Any:
            calls.append(args)
            raise ClientConnectionError("synthetic failure")

        monkeypatch.setattr(session, "request", failed)
        with pytest.raises(KepcoOnConnectionError):
            await PowerPlannerClient(session)._request("POST", path, form={})
        assert len(calls) == 1


def test_daily_hour_range_cannot_have_a_gap() -> None:
    intervals = chart()
    intervals[12]["MR_HHMI"] = "24시"
    with pytest.raises(KepcoOnProtocolError):
        parse_daily([{"F_AP_QT": "25"}], intervals, completed=True)


async def test_opaque_session_cookie_is_persisted_without_restricting_to_letters() -> None:
    cookie = "SYNTHETIC+OPAQUE/TOKEN==:NODE!1"
    store = MemoryStore()
    async with ClientSession(cookie_jar=CookieJar(quote_cookie=False)) as session:
        session.cookie_jar.update_cookies({"JSESSIONID": cookie}, response_url=ORIGIN)
        await PowerPlannerClient(session, store)._persist()
        assert store.saved == [cookie]


async def test_disable_storage_failure_leaves_options_active(
    option_fakes: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnOptionsFlow

    from .test_config_flow import attach_fake_hass, make_entry

    async def fail_clear() -> None:
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(option_fakes["store"], "async_clear", fail_clear)
    entry = make_entry()
    entry.options = {OPT_ENABLE_POWER_PLANNER: True, OPT_POWER_PLANNER_CUSTOMER_ID: "key-1"}
    flow = KepcoOnOptionsFlow(cast("Any", entry))
    attach_fake_hass(flow)
    result = await flow.async_step_init({OPT_ENABLE_POWER_PLANNER: False})
    assert result["errors"] == {"base": "unknown"}
    assert entry.options[OPT_ENABLE_POWER_PLANNER]


async def test_planner_platform_reuses_old_ids_and_does_not_replace_other_customer(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.kepco_on import sensor
    from custom_components.kepco_on.models import KepcoCoordinatorData

    from .test_sensor import bill, customer, entry

    customers = (customer("bound"), customer("other"))
    coordinator = FakeCoordinator(
        KepcoCoordinatorData(
            customers=customers,
            bills_by_customer_key={item.stable_key: bill() for item in customers},
            errors_by_customer_key={},
        )
    )
    config_entry = entry(
        coordinator=coordinator,
        options={OPT_ENABLE_POWER_PLANNER: True, OPT_POWER_PLANNER_CUSTOMER_ID: "bound"},
    )
    planner = SimpleNamespace(data=PowerPlannerData({}), last_update_success=True)
    config_entry.runtime_data.power_planner = planner
    entities: list[Any] = []
    await sensor.async_setup_entry(hass, cast("Any", config_entry), cast("Any", entities.extend))
    bound = [entity for entity in entities if entity.customer.stable_key == "bound"]
    other = [entity for entity in entities if entity.customer.stable_key == "other"]
    assert len(bound) == 56
    assert len(other) == 34
    assert len({entity.unique_id for entity in entities}) == 90
    assert all(isinstance(entity, sensor.KepcoOnSensor) for entity in other)
    assert len([entity for entity in bound if isinstance(entity, PowerPlannerSensor)]) == 24
    current = next(
        entity for entity in bound if entity.entity_description.key == "current_period_usage"
    )
    assert current.unique_id == "bound_current_period_usage"
    old_description = next(
        description
        for description in sensor.SENSOR_DESCRIPTIONS
        if description.key == "current_period_usage"
    )
    old = sensor.KepcoOnSensor(cast("Any", coordinator), customers[0], old_description, {})
    assert current.device_info == old.device_info


@pytest.mark.parametrize("data", [None, PowerPlannerData({})])
async def test_missing_coordinator_data_is_unknown(data: object) -> None:
    coordinator = SimpleNamespace(data=data, last_update_success=False)
    entity = PowerPlannerSensor(
        cast("Any", coordinator), _house_customer(), PLANNER_DESCRIPTIONS[0]
    )
    assert entity.native_value is None
    assert entity.extra_state_attributes["data_status"] == "connection_error"


@pytest.mark.parametrize("enabled", [True, False])
async def test_optional_registry_toggle_preserves_user_disabled_entities(
    hass: HomeAssistant,
    enabled: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.kepco_on import sensor
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.entity_registry import RegistryEntryDisabler

    from .test_sensor import FakeDeviceRegistry, FakeEntityRegistry, entity_entry

    registry = FakeEntityRegistry(
        [
            entity_entry(
                f"sensor.{key}",
                f"house-key_{key}",
                config_entry_id="planner",
                disabled_by=disabled,
            )
            for key, disabled in [
                ("power_planner_today_usage", None),
                ("power_planner_yesterday_usage", RegistryEntryDisabler.USER),
                ("power_planner_current_amount", RegistryEntryDisabler.INTEGRATION),
            ]
        ]
    )
    monkeypatch.setattr(er, "async_get", lambda hass: registry)
    monkeypatch.setattr(er, "async_entries_for_config_entry", lambda reg, entry_id: reg.entries)
    devices = FakeDeviceRegistry([])
    monkeypatch.setattr(dr, "async_get", lambda hass: devices)
    monkeypatch.setattr(dr, "async_entries_for_config_entry", lambda reg, entry_id: reg.entries)
    entry = SimpleNamespace(
        entry_id="planner",
        options={OPT_ENABLE_POWER_PLANNER: enabled, OPT_POWER_PLANNER_CUSTOMER_ID: "house-key"},
    )
    await sensor._async_remove_stale_registry_entries(
        hass, cast("Any", entry), (_house_customer(),)
    )
    assert registry.removed == []
    assert registry.updated == (
        [("sensor.power_planner_current_amount", None)]
        if enabled
        else [("sensor.power_planner_today_usage", RegistryEntryDisabler.INTEGRATION)]
    )


@pytest.mark.parametrize("bound", [True, False])
async def test_site_binding_skips_only_same_customer_on_planner(bound: bool) -> None:
    from custom_components.kepco_on.api import KepcoOnClient
    from custom_components.kepco_on.const import ENDPOINT_POWER_PLANNER

    from .test_api_house import HouseAuth

    auth = HouseAuth()
    client = KepcoOnClient(
        auth,
        planner_excluded_customer_keys=frozenset({"house-key"})
        if bound
        else frozenset({"another-key"}),
    )
    bill = await client.async_get_bill(_house_customer())
    assert bill.bill_month == "202608"
    assert bill.usage_kwh == 603
    assert bill.amount_krw == 118810
    assert bill.charge.base_krw == 6060
    assert any(path == ENDPOINT_POWER_PLANNER for path, _, _ in auth.calls) == (not bound)
    if bound:
        assert bill.current_period_usage_kwh is None


@pytest.mark.parametrize("already_enabled", [False, True])
async def test_expired_planner_session_prompts_only_when_authentication_is_needed(
    option_fakes: dict[str, Any],
    already_enabled: bool,
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnOptionsFlow
    from custom_components.kepco_on.const import OPT_POLLING_INTERVAL_HOURS

    from .test_config_flow import attach_fake_hass, make_entry

    option_fakes["store"].session_id = "TEST_SESSION"
    option_fakes["error"] = KepcoOnSessionExpired("PRIVATE")
    entry = make_entry()
    if already_enabled:
        entry.options = {OPT_ENABLE_POWER_PLANNER: True, OPT_POWER_PLANNER_CUSTOMER_ID: "key-1"}
        entry.runtime_data = SimpleNamespace(
            power_planner=SimpleNamespace(
                data=PowerPlannerData({"key-1": PowerPlannerSnapshot(status="auth_required")})
            )
        )
    original_data = dict(entry.data)
    original_options = dict(entry.options)
    flow = KepcoOnOptionsFlow(cast("Any", entry))
    attach_fake_hass(flow)
    result = await flow.async_step_init(
        {OPT_ENABLE_POWER_PLANNER: True, OPT_POLLING_INTERVAL_HOURS: 3}
    )
    assert result["step_id"] == "power_planner_auth"
    assert option_fakes["logins"] == []
    assert entry.data == original_data
    assert entry.options == original_options
    option_fakes["error"] = None
    result = await flow.async_step_power_planner_auth({CONF_PASSWORD: "ONE_TIME_PASSWORD"})
    assert result["type"] == "create_entry"
    assert result["data"][OPT_POLLING_INTERVAL_HOURS] == 3
    assert result["data"][OPT_POWER_PLANNER_CUSTOMER_ID] == "key-1"
    assert CONF_PASSWORD not in result["data"]
    assert entry.data == original_data
    assert flow._planner_options is None
    assert all(session.closed for session in option_fakes["sessions"])


@pytest.mark.parametrize("password", ["", None, 123])
async def test_conditional_auth_rejects_empty_or_invalid_password_without_network(
    option_fakes: dict[str, Any],
    password: object,
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnOptionsFlow

    from .test_config_flow import attach_fake_hass, make_entry

    entry = make_entry()
    flow = KepcoOnOptionsFlow(cast("Any", entry))
    attach_fake_hass(flow)
    await flow.async_step_init({OPT_ENABLE_POWER_PLANNER: True})
    result = await flow.async_step_power_planner_auth({CONF_PASSWORD: password})
    assert result["step_id"] == "power_planner_auth"
    assert result["errors"] == {"base": "invalid_auth"}
    assert option_fakes["logins"] == []
    assert not entry.options


async def test_auth_step_without_pending_options_returns_main_form(
    option_fakes: dict[str, Any],
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnOptionsFlow

    from .test_config_flow import attach_fake_hass, make_entry

    flow = KepcoOnOptionsFlow(cast("Any", make_entry()))
    attach_fake_hass(flow)
    result = await flow.async_step_power_planner_auth({CONF_PASSWORD: "UNEXPECTED_PASSWORD"})
    assert result["step_id"] == "init"
    assert option_fakes["sessions"] == []


async def test_corrupt_planner_store_allows_conditional_sign_in(
    option_fakes: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnOptionsFlow

    from .test_config_flow import attach_fake_hass, make_entry

    async def bad_load() -> str | None:
        raise KepcoOnProtocolError("PRIVATE")

    monkeypatch.setattr(option_fakes["store"], "async_load", bad_load)
    flow = KepcoOnOptionsFlow(cast("Any", make_entry()))
    attach_fake_hass(flow)
    result = await flow.async_step_init({OPT_ENABLE_POWER_PLANNER: True})
    assert result["step_id"] == "power_planner_auth"
    assert option_fakes["store"].clears == 1
    result = await flow.async_step_power_planner_auth({CONF_PASSWORD: "ONE_TIME_PASSWORD"})
    assert result["type"] == "create_entry"
    assert CONF_PASSWORD not in result["data"]
