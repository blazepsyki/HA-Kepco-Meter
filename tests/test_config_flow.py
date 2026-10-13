"""Config flow contract tests for KEPCO ON."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast
from unittest.mock import Mock

import pytest
from aiohttp import CookieJar
from custom_components.kepco_on.const import (
    CONF_ACCOUNT_UID_HASH,
    CONF_CUSTOMERS,
    CONF_DISPLAY_NAME,
    CONF_SAVE_PASSWORD,
    CONF_SELECTED_CUSTOMERS,
    CONF_SESSION_HANDOFF,
    CONF_USERNAME,
    CONFIG_ENTRY_VERSION,
    DATA_CUSTOMER_NUMBER,
    DATA_HOUSE_CONTRACT_NUMBER,
    DEFAULT_CO2_FACTOR_KG_PER_KWH,
    DEFAULT_POLLING_INTERVAL_HOURS,
    DOMAIN,
    OPT_CO2_FACTOR_KG_PER_KWH,
    OPT_HISTORY_MONTHS,
    OPT_POLLING_INTERVAL_HOURS,
    SENSITIVE_CONFIG_DATA_KEYS,
)
from custom_components.kepco_on.exceptions import (
    KepcoOnAuthError,
    KepcoOnConnectionError,
    KepcoOnMfaRequired,
    KepcoOnNoCustomersError,
    KepcoOnProtocolError,
    KepcoOnRateLimitError,
    KepcoOnUnsupportedAccount,
)
from custom_components.kepco_on.models import KepcoAccountSession, KepcoCustomer
from custom_components.kepco_on.parser import CustomerRowSkips
from homeassistant.config_entries import (
    ConfigEntryState,
    OptionsFlowManager,
)
from homeassistant.const import CONF_PASSWORD
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.redact import REDACTED, async_redact_data
from homeassistant.helpers.typing import UNDEFINED
from homeassistant.helpers.update_coordinator import UpdateFailed

ROOT = Path(__file__).resolve().parents[1]
PASSWORD_SECRET = "PASSWORD_SECRET_CANARY"
TOKEN_SECRET = "TOKEN_SECRET_CANARY"
RAW_CUSTOMER_SECRET = "CUST_NO_SECRET_CANARY"
RAW_HOUSE_SECRET = "HOUSE_NO_SECRET_CANARY"


class FakeFlowManager:
    """Minimal config entry flow manager used by async_set_unique_id."""

    def async_progress_by_handler(
        self,
        handler: str,
        include_uninitialized: bool = False,
        match_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        del handler, include_uninitialized, match_context
        return []

    def async_abort(self, flow_id: str) -> None:
        del flow_id


class FakeConfigEntries:
    """Minimal config entries facade for direct flow unit tests."""

    def __init__(self) -> None:
        self.flow = FakeFlowManager()
        self.entries_by_id: dict[str, FakeConfigEntry] = {}
        self.entries_by_unique_id: dict[str, FakeConfigEntry] = {}
        self.updates: list[tuple[FakeConfigEntry, dict[str, Any]]] = []
        self.reloads: list[str] = []

    def async_get_known_entry(self, entry_id: str) -> FakeConfigEntry:
        return self.entries_by_id[entry_id]

    def async_entry_for_domain_unique_id(
        self, domain: str, unique_id: str
    ) -> FakeConfigEntry | None:
        assert domain == DOMAIN
        return self.entries_by_unique_id.get(unique_id)

    def async_update_entry(
        self,
        entry: FakeConfigEntry,
        *,
        unique_id: Any = None,
        title: Any = None,
        data: Any = None,
        options: Any = None,
        **_: Any,
    ) -> bool:
        if unique_id is not UNDEFINED and unique_id is not None:
            entry.unique_id = unique_id
        if title is not UNDEFINED and title is not None:
            entry.title = title
        if data is not UNDEFINED and data is not None:
            entry.data = dict(data)
        if options is not UNDEFINED and options is not None:
            entry.options = dict(options)
        self.updates.append((entry, {} if data is UNDEFINED or data is None else dict(data)))
        return True

    def async_schedule_reload(self, entry_id: str) -> None:
        self.reloads.append(entry_id)


class FakeHass:
    """Small hass object with only config_entries and loop touched by the flow."""

    def __init__(self) -> None:
        self.config_entries = FakeConfigEntries()
        self.loop = None
        self.created_tasks: list[asyncio.Task[None]] = []

    def async_create_task(self, target: Any) -> asyncio.Task[None]:
        task = asyncio.create_task(target)
        self.created_tasks.append(task)
        return task


class FakeConfigEntry:
    """Small mutable config entry for reauth/reconfigure/options tests."""

    def __init__(
        self,
        *,
        data: Mapping[str, Any],
        options: Mapping[str, Any] | None = None,
        unique_id: str | None = None,
        title: str = "한전ON",
        entry_id: str = "entry-1",
    ) -> None:
        self.data = dict(data)
        self.options = dict(options or {})
        self.unique_id: str | None = unique_id or cast("str", data[CONF_ACCOUNT_UID_HASH])
        self.title = title
        self.entry_id = entry_id
        self.update_listeners: list[Any] = []
        self.state: ConfigEntryState | None = None
        self.source = "user"
        self.runtime_data: Any = None


class FakeSession:
    """Session returned by the patched HA session factory."""

    def __init__(self) -> None:
        self.cookie_jar = CookieJar()
        self.closed: bool = False

    def detach(self) -> None:
        self.closed = True


class FailingDetachSession(FakeSession):
    """Session that fails on detach without exposing secrets in the exception text."""

    def detach(self) -> None:
        raise RuntimeError("detach failed")


class MemoryStore:
    """Store used by the flow auth object."""

    def __init__(self) -> None:
        self.session: KepcoAccountSession | None = None

    async def async_load(self) -> KepcoAccountSession | None:
        return self.session

    async def async_save(self, session: KepcoAccountSession) -> None:
        self.session = session

    async def async_clear(self) -> None:
        self.session = None


class FakeAuth:
    """Auth mock that keeps the same public surface as KepcoOnAuth."""

    login_results: ClassVar[list[KepcoAccountSession | Exception]] = []
    login_calls: ClassVar[list[tuple[str, str]]] = []
    instances: ClassVar[list[FakeAuth]] = []

    def __init__(
        self,
        session: FakeSession,
        *,
        store: MemoryStore,
        reauth_username: str | None = None,
        reauth_password: str | None = None,
    ) -> None:
        self.session = session
        self.store = store
        self.reauth_username = reauth_username
        self.reauth_password = reauth_password
        self.current_session: KepcoAccountSession | None = None
        FakeAuth.instances.append(self)

    async def async_login(self, username: str, password: str) -> KepcoAccountSession:
        FakeAuth.login_calls.append((username, password))
        result = FakeAuth.login_results.pop(0)
        if isinstance(result, Exception):
            raise result
        self.current_session = result
        await self.store.async_save(result)
        return result

    async def async_export_session_snapshot(self) -> KepcoAccountSession:
        assert self.current_session is not None
        return self.current_session

    def account_uid_hash(self) -> str:
        assert self.current_session is not None
        return f"client-{self.current_session.user_id}"


class FakeClient:
    """Client mock for account type and customers."""

    account_results: ClassVar[list[str | Exception]] = ["INDI"]
    customer_results: ClassVar[list[tuple[KepcoCustomer, ...] | Exception]] = []
    customer_row_skips: ClassVar[CustomerRowSkips] = CustomerRowSkips()

    def __init__(self, auth: FakeAuth) -> None:
        self.auth = auth

    async def async_get_account_type(self) -> str:
        result = FakeClient.account_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def async_get_customers(self) -> tuple[KepcoCustomer, ...]:
        result = FakeClient.customer_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def patched_flow_dependencies(monkeypatch: pytest.MonkeyPatch) -> list[FakeSession]:
    """Patch network/session dependencies at the config-flow seam."""
    import custom_components.kepco_on.config_flow as config_flow

    sessions: list[FakeSession] = []

    def make_session(
        hass: FakeHass,
        *,
        auto_cleanup: bool,
        cookie_jar: CookieJar,
    ) -> FakeSession:
        del hass
        assert auto_cleanup is False
        session = FakeSession()
        session.cookie_jar = cookie_jar
        sessions.append(session)
        return session

    FakeAuth.login_results = [account_session()]
    FakeAuth.login_calls = []
    FakeAuth.instances = []
    FakeClient.account_results = ["INDI"]
    FakeClient.customer_results = [(customer("key-1"),)]
    FakeClient.customer_row_skips = CustomerRowSkips()
    monkeypatch.setattr(config_flow, "async_create_clientsession", make_session)
    monkeypatch.setattr(config_flow, "KepcoOnAuth", FakeAuth)
    monkeypatch.setattr(config_flow, "KepcoOnClient", FakeClient)
    return sessions


def account_hash(user_id: str = "SERVER_USER") -> str:
    return hashlib.sha256(f"kepco_on:{user_id.strip()}".encode()).hexdigest()


def account_session(user_id: str = "SERVER_USER") -> KepcoAccountSession:
    return KepcoAccountSession(
        refresh_token=TOKEN_SECRET,
        token="ACCESS_SECRET_CANARY",
        user_id=f" {user_id} ",
        member_name="Member Secret",
        user_mng_seqno="SEQ_SECRET",
        updated_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


def customer(
    stable_key: str,
    *,
    apartment: str = "푸른아파트",
    dong: str = "101",
    ho: str = "1001",
    customer_number: str = RAW_CUSTOMER_SECRET,
    house_contract_number: str = RAW_HOUSE_SECRET,
) -> KepcoCustomer:
    return KepcoCustomer(
        stable_key=stable_key,
        apartment_name=apartment,
        dong=dong,
        ho=ho,
        contract_method="아파트(단일계약)",
        is_supported=True,
        _customer_number=customer_number,
        _house_contract_number=house_contract_number,
    )


def make_flow() -> Any:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    flow = KepcoOnConfigFlow()
    attach_fake_hass(flow)
    flow.context = {"source": "user"}
    flow.flow_id = "flow-1"
    return flow


def attach_fake_hass(flow: Any) -> FakeHass:
    fake_hass = FakeHass()
    flow.hass = fake_hass
    flow.handler = DOMAIN
    return fake_hass


async def submit_user(flow: Any, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        CONF_USERNAME: "input-user",
        CONF_PASSWORD: PASSWORD_SECRET,
        CONF_SAVE_PASSWORD: False,
        CONF_DISPLAY_NAME: "",
    }
    payload.update(overrides)
    return cast("dict[str, Any]", await flow.async_step_user(payload))


async def reach_customer_step(flow: Any, **overrides: Any) -> dict[str, Any]:
    result = await submit_user(flow, **overrides)
    assert result["type"] == "form"
    assert result["step_id"] == "customer"
    return result


async def create_entry(flow: Any, selected: list[str] | None = None) -> dict[str, Any]:
    await reach_customer_step(flow)
    return cast(
        "dict[str, Any]",
        await flow.async_step_customer({CONF_SELECTED_CUSTOMERS: selected or ["key-1"]}),
    )


@pytest.mark.asyncio
async def test_flow_session_store_load_save_clear() -> None:
    from custom_components.kepco_on.config_flow import FlowSessionStore

    store = FlowSessionStore()
    session = account_session()

    assert await store.async_load() is None
    await store.async_save(session)
    assert await store.async_load() == session
    await store.async_clear()
    assert await store.async_load() is None


def test_config_flow_provides_options_flow() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow, KepcoOnOptionsFlow

    options_flow = KepcoOnConfigFlow.async_get_options_flow(cast("Any", make_entry()))

    assert isinstance(options_flow, KepcoOnOptionsFlow)


@pytest.mark.asyncio
async def test_user_and_customer_steps_show_initial_forms() -> None:
    flow = make_flow()

    user_form = await flow.async_step_user()
    customer_recovery = await flow.async_step_customer()
    await reach_customer_step(flow)
    customer_form = await flow.async_step_customer()

    assert user_form["type"] == "form"
    assert user_form["step_id"] == "user"
    assert customer_recovery["type"] == "form"
    assert customer_recovery["step_id"] == "user"
    assert customer_form["type"] == "form"
    assert customer_form["step_id"] == "customer"


@pytest.mark.asyncio
async def test_user_success_then_customer_creates_private_entry(
    patched_flow_dependencies: list[FakeSession],
) -> None:
    flow = make_flow()

    await reach_customer_step(
        flow,
        **{CONF_USERNAME: "typed-user", CONF_DISPLAY_NAME: "My Home"},
    )
    result = await flow.async_step_customer({CONF_SELECTED_CUSTOMERS: ["key-1"]})

    assert result["type"] == "create_entry"
    assert result["title"] == "My Home"
    data = result["data"]
    assert data[CONF_USERNAME] == "typed-user"
    assert CONF_PASSWORD not in data
    assert data[CONF_SAVE_PASSWORD] is False
    assert data[CONF_ACCOUNT_UID_HASH] == account_hash("SERVER_USER")
    assert data[CONF_SELECTED_CUSTOMERS] == ["key-1"]
    assert len(data[CONF_CUSTOMERS]) == 1
    assert data[CONF_CUSTOMERS][0][DATA_CUSTOMER_NUMBER] == RAW_CUSTOMER_SECRET
    assert data[CONF_CUSTOMERS][0][DATA_HOUSE_CONTRACT_NUMBER] == RAW_HOUSE_SECRET
    assert data[CONF_SESSION_HANDOFF]["cookies"] == []
    assert TOKEN_SECRET in json.dumps(data[CONF_SESSION_HANDOFF])
    assert flow.context["unique_id"] == account_hash("SERVER_USER")
    assert patched_flow_dependencies[0].closed is True
    redacted = async_redact_data(data, SENSITIVE_CONFIG_DATA_KEYS)
    assert redacted[CONF_SESSION_HANDOFF] == REDACTED


@pytest.mark.asyncio
async def test_default_title_uses_normalized_selected_location() -> None:
    FakeClient.customer_results = [(customer("key-1", dong="1001", ho="0101"),)]
    flow = make_flow()

    result = await create_entry(flow)

    assert result["type"] == "create_entry"
    assert result["title"] == "1001동 101호"


@pytest.mark.asyncio
async def test_save_password_true_stores_password() -> None:
    flow = make_flow()

    await reach_customer_step(flow, **{CONF_SAVE_PASSWORD: True})
    result = await flow.async_step_customer({CONF_SELECTED_CUSTOMERS: ["key-1"]})

    assert result["data"][CONF_PASSWORD] == PASSWORD_SECRET
    redacted = async_redact_data(result["data"], SENSITIVE_CONFIG_DATA_KEYS)
    assert redacted[CONF_PASSWORD] == REDACTED


@pytest.mark.asyncio
async def test_customer_labels_use_only_apartment_dong_ho() -> None:
    FakeClient.customer_results = [(customer("key-1", dong="1001", ho="0101"),)]
    flow = make_flow()

    result = await reach_customer_step(flow)

    data_schema = result["data_schema"]
    assert data_schema is not None
    selector_obj = next(iter(data_schema.schema.values()))
    labels = str(selector_obj.config)
    assert "푸른아파트 1001동 101호" in labels
    assert "0101호" not in labels
    assert RAW_CUSTOMER_SECRET not in labels
    assert RAW_HOUSE_SECRET not in labels
    assert "Member Secret" not in labels


@pytest.mark.asyncio
async def test_multiple_customers_and_empty_selection_recovery(
    patched_flow_dependencies: list[FakeSession],
) -> None:
    FakeClient.customer_results = [(customer("key-1"), customer("key-2", apartment="별빛아파트"))]
    flow = make_flow()
    await reach_customer_step(flow)

    empty = await flow.async_step_customer({CONF_SELECTED_CUSTOMERS: []})
    assert empty["type"] == "form"
    assert empty["errors"] == {"base": "invalid_selection"}
    assert patched_flow_dependencies[0].closed is False

    result = await flow.async_step_customer({CONF_SELECTED_CUSTOMERS: ["key-1", "key-2"]})
    assert result["type"] == "create_entry"
    assert result["data"][CONF_SELECTED_CUSTOMERS] == ["key-1", "key-2"]
    assert result["title"] == "101동 1001호 외 1세대"
    assert patched_flow_dependencies[0].closed is True


@pytest.mark.asyncio
async def test_customer_entry_stores_raw_ids_only_for_selected_customer() -> None:
    FakeClient.customer_results = [(customer("key-1"), customer("key-2", apartment="별빛아파트"))]
    flow = make_flow()
    await reach_customer_step(flow)

    result = await flow.async_step_customer({CONF_SELECTED_CUSTOMERS: ["key-1"]})

    serialized = result["data"][CONF_CUSTOMERS]
    assert [item["stable_key"] for item in serialized] == ["key-1"]
    rendered_data = json.dumps(result["data"], ensure_ascii=False)
    assert "CUST2" not in rendered_data
    assert "HOUSE2" not in rendered_data


@pytest.mark.asyncio
async def test_customer_step_rejects_duplicate_selected_keys() -> None:
    flow = make_flow()
    await reach_customer_step(flow)

    result = await flow.async_step_customer({CONF_SELECTED_CUSTOMERS: ["key-1", "key-1"]})

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_selection"}


@pytest.mark.asyncio
async def test_flow_abandonment_closes_pending_login_session(
    patched_flow_dependencies: list[FakeSession],
) -> None:
    flow = make_flow()
    fake_hass = cast("FakeHass", flow.hass)
    await reach_customer_step(flow)
    assert not bool(patched_flow_dependencies[0].closed)
    assert cast("Any", flow)._pending is not None

    remove_flow = cast("Any", flow.async_remove)
    remove_flow()
    remove_flow()
    assert len(fake_hass.created_tasks) == 1
    await fake_hass.created_tasks[0]

    assert bool(patched_flow_dependencies[0].closed)
    assert cast("Any", flow)._pending is None


@pytest.mark.asyncio
async def test_flow_abandonment_close_failure_is_observed_without_secret_logging(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.kepco_on.config_flow as config_flow

    sessions: list[FailingDetachSession] = []

    def make_session(
        hass: FakeHass,
        *,
        auto_cleanup: bool,
        cookie_jar: CookieJar,
    ) -> FailingDetachSession:
        del hass, auto_cleanup
        session = FailingDetachSession()
        session.cookie_jar = cookie_jar
        sessions.append(session)
        return session

    monkeypatch.setattr(config_flow, "async_create_clientsession", make_session)
    caplog.set_level(logging.DEBUG)
    flow = make_flow()
    fake_hass = cast("FakeHass", flow.hass)
    await reach_customer_step(flow)

    flow.async_remove()
    await fake_hass.created_tasks[0]

    assert sessions
    assert "Failed to close KEPCO ON config flow session" in caplog.text
    assert PASSWORD_SECRET not in caplog.text
    assert TOKEN_SECRET not in caplog.text


@pytest.mark.asyncio
async def test_user_step_errors_close_session_and_retry_can_succeed(
    patched_flow_dependencies: list[FakeSession],
) -> None:
    FakeAuth.login_results = [KepcoOnAuthError(PASSWORD_SECRET), account_session()]
    flow = make_flow()

    failed = await submit_user(flow)
    assert failed["type"] == "form"
    assert failed["errors"] == {"base": "invalid_auth"}
    assert patched_flow_dependencies[0].closed is True

    retried = await submit_user(flow)
    assert retried["type"] == "form"
    assert retried["step_id"] == "customer"
    assert patched_flow_dependencies[1].closed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "error"),
    [
        (KepcoOnConnectionError("down"), "cannot_connect"),
        (KepcoOnRateLimitError("slow"), "rate_limited"),
        (KepcoOnUnsupportedAccount("corp"), "unsupported_account"),
        (KepcoOnNoCustomersError("none"), "no_customers"),
        (KepcoOnMfaRequired("mfa"), "mfa_required"),
        (KepcoOnProtocolError("shape"), "protocol_changed_login"),
    ],
)
async def test_user_step_maps_validation_errors(
    patched_flow_dependencies: list[FakeSession],
    raised: Exception,
    error: str,
) -> None:
    if isinstance(raised, KepcoOnNoCustomersError):
        FakeClient.customer_results = [()]
    elif isinstance(raised, KepcoOnUnsupportedAccount):
        FakeClient.account_results = [raised]
    elif isinstance(raised, KepcoOnConnectionError | KepcoOnMfaRequired | KepcoOnProtocolError):
        FakeAuth.login_results = [raised]
    else:
        FakeAuth.login_results = [raised]
    flow = make_flow()

    result = await submit_user(flow)

    assert result["type"] == "form"
    assert result["errors"] == {"base": error}
    assert patched_flow_dependencies[0].closed is True


@pytest.mark.asyncio
async def test_user_step_account_type_protocol_error_is_distinguished(
    patched_flow_dependencies: list[FakeSession],
) -> None:
    FakeClient.account_results = [KepcoOnProtocolError("account type shape changed")]
    flow = make_flow()

    result = await submit_user(flow)

    assert result["type"] == "form"
    assert result["errors"] == {"base": "protocol_changed_account_type"}
    assert patched_flow_dependencies[0].closed is True


@pytest.mark.asyncio
async def test_user_step_protocol_log_contains_only_safe_stage_and_field(
    patched_flow_dependencies: list[FakeSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    username = "USER_SECRET_CANARY"
    password = "PASSWORD_SECRET_CANARY"
    FakeClient.customer_results = [KepcoOnProtocolError("APT_NAME is missing")]
    flow = make_flow()

    with caplog.at_level(logging.ERROR, logger="custom_components.kepco_on.config_flow"):
        result = await submit_user(
            flow,
            **{
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
                CONF_SAVE_PASSWORD: False,
            },
        )

    assert result["errors"] == {"base": "protocol_changed_customers"}
    assert patched_flow_dependencies[0].closed is True
    assert "during customers" in caplog.text
    assert "KepcoOnProtocolError" in caplog.text
    assert "APT_NAME is missing" in caplog.text
    assert caplog.records[-1].levelno == logging.ERROR
    assert username not in caplog.text
    assert password not in caplog.text


@pytest.mark.asyncio
async def test_user_step_connection_log_is_error_and_contains_no_credentials(
    patched_flow_dependencies: list[FakeSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    username = "USER_SECRET_CANARY"
    password = "PASSWORD_SECRET_CANARY"
    FakeAuth.login_results = [KepcoOnConnectionError("KEPCO ON returned HTTP 500")]
    flow = make_flow()

    with caplog.at_level(logging.ERROR, logger="custom_components.kepco_on.config_flow"):
        result = await submit_user(
            flow,
            **{
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
                CONF_SAVE_PASSWORD: False,
            },
        )

    assert result["errors"] == {"base": "cannot_connect"}
    assert patched_flow_dependencies[0].closed is True
    assert "during login" in caplog.text
    assert "KepcoOnConnectionError" in caplog.text
    assert "HTTP 500" in caplog.text
    assert caplog.records[-1].levelno == logging.ERROR
    assert username not in caplog.text
    assert password not in caplog.text


@pytest.mark.asyncio
async def test_user_step_unexpected_error_propagates_after_cleanup(
    patched_flow_dependencies: list[FakeSession],
) -> None:
    FakeAuth.login_results = [RuntimeError("boom")]
    flow = make_flow()

    with pytest.raises(RuntimeError, match="boom"):
        await submit_user(flow)

    assert patched_flow_dependencies[0].closed is True
    assert cast("Any", flow)._pending is None


@pytest.mark.asyncio
async def test_duplicate_account_aborts_after_server_user_id_hash() -> None:
    flow = make_flow()
    flow.hass.config_entries.entries_by_unique_id[account_hash("SERVER_USER")] = FakeConfigEntry(
        data={
            CONF_USERNAME: "old-user",
            CONF_SAVE_PASSWORD: False,
            CONF_ACCOUNT_UID_HASH: account_hash("SERVER_USER"),
            CONF_CUSTOMERS: [],
            CONF_SELECTED_CUSTOMERS: [],
        }
    )

    result = await submit_user(flow, **{CONF_USERNAME: "different-input"})

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_user_schema_uses_text_selectors_for_username_display_and_password() -> None:
    flow = make_flow()

    result = await flow.async_step_user()
    schema = result["data_schema"].schema
    schema_by_key = {key.schema: value for key, value in schema.items()}

    assert isinstance(schema_by_key[CONF_USERNAME], object)
    assert schema_by_key[CONF_USERNAME].selector_type == "text"
    assert "type" not in schema_by_key[CONF_USERNAME].config
    assert schema_by_key[CONF_PASSWORD].selector_type == "text"
    assert schema_by_key[CONF_PASSWORD].config["type"] == "password"
    assert schema_by_key[CONF_DISPLAY_NAME].selector_type == "text"
    assert "type" not in schema_by_key[CONF_DISPLAY_NAME].config


@pytest.mark.asyncio
async def test_session_handoff_and_errors_do_not_leak_to_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    flow = make_flow()
    await reach_customer_step(flow)
    result = await flow.async_step_customer({CONF_SELECTED_CUSTOMERS: ["key-1"]})

    rendered_result = repr(result)
    rendered_logs = caplog.text
    assert TOKEN_SECRET in json.dumps(result["data"][CONF_SESSION_HANDOFF])
    assert TOKEN_SECRET not in rendered_logs
    assert PASSWORD_SECRET not in rendered_logs
    assert RAW_CUSTOMER_SECRET not in rendered_logs
    assert RAW_HOUSE_SECRET not in rendered_logs
    assert "cookies" in rendered_result


def make_entry() -> FakeConfigEntry:
    return FakeConfigEntry(
        unique_id=account_hash("SERVER_USER"),
        data={
            CONF_USERNAME: "input-user",
            CONF_SAVE_PASSWORD: False,
            CONF_ACCOUNT_UID_HASH: account_hash("SERVER_USER"),
            CONF_CUSTOMERS: [
                {
                    "stable_key": "key-1",
                    "apartment_name": "푸른아파트",
                    "dong": "101",
                    "ho": "1001",
                    "contract_method": "아파트(단일계약)",
                    "is_supported": True,
                    DATA_CUSTOMER_NUMBER: RAW_CUSTOMER_SECRET,
                    DATA_HOUSE_CONTRACT_NUMBER: RAW_HOUSE_SECRET,
                },
                {
                    "stable_key": "key-2",
                    "apartment_name": "별빛아파트",
                    "dong": "102",
                    "ho": "1002",
                    "contract_method": "아파트(종합계약)",
                    "is_supported": True,
                    DATA_CUSTOMER_NUMBER: "CUST2",
                    DATA_HOUSE_CONTRACT_NUMBER: "HOUSE2",
                },
            ],
            CONF_SELECTED_CUSTOMERS: ["key-1"],
        },
    )


def test_config_flow_uses_current_entry_version() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    assert KepcoOnConfigFlow.VERSION == CONFIG_ENTRY_VERSION == 3


@pytest.mark.asyncio
async def test_options_flow_accepts_valid_values() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnOptionsFlow

    entry = make_entry()
    flow = KepcoOnOptionsFlow(cast("Any", entry))
    attach_fake_hass(flow)

    result = await flow.async_step_init(
        {
            OPT_POLLING_INTERVAL_HOURS: 12,
            OPT_CO2_FACTOR_KG_PER_KWH: 0.5,
            OPT_HISTORY_MONTHS: 24,
        }
    )

    assert result["type"] == "create_entry"
    assert result["data"] == {
        OPT_POLLING_INTERVAL_HOURS: 12,
        OPT_CO2_FACTOR_KG_PER_KWH: 0.5,
        OPT_HISTORY_MONTHS: 24,
    }


@pytest.mark.asyncio
async def test_options_flow_rejects_invalid_values() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnOptionsFlow

    entry = make_entry()
    flow = KepcoOnOptionsFlow(cast("Any", entry))
    attach_fake_hass(flow)

    valid = {
        OPT_POLLING_INTERVAL_HOURS: 6,
        OPT_CO2_FACTOR_KG_PER_KWH: DEFAULT_CO2_FACTOR_KG_PER_KWH,
        OPT_HISTORY_MONTHS: 12,
    }
    for payload, error in (
        ({**valid, OPT_POLLING_INTERVAL_HOURS: "bad"}, "unknown"),
        ({**valid, OPT_POLLING_INTERVAL_HOURS: 2}, "unknown"),
        ({**valid, OPT_CO2_FACTOR_KG_PER_KWH: "bad"}, "invalid_co2_factor"),
        ({**valid, OPT_CO2_FACTOR_KG_PER_KWH: 0}, "invalid_co2_factor"),
        ({**valid, OPT_CO2_FACTOR_KG_PER_KWH: 10.1}, "invalid_co2_factor"),
        ({**valid, OPT_CO2_FACTOR_KG_PER_KWH: "NaN"}, "invalid_co2_factor"),
        ({**valid, OPT_CO2_FACTOR_KG_PER_KWH: "Infinity"}, "invalid_co2_factor"),
        ({**valid, OPT_HISTORY_MONTHS: "bad"}, "invalid_history_months"),
        ({**valid, OPT_HISTORY_MONTHS: 0}, "invalid_history_months"),
        ({**valid, OPT_HISTORY_MONTHS: 25}, "invalid_history_months"),
    ):
        result = await flow.async_step_init(payload)
        assert result["type"] == "form"
        assert result["errors"] == {"base": error}


@pytest.mark.asyncio
async def test_options_flow_default_form_values() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnOptionsFlow

    flow = KepcoOnOptionsFlow(cast("Any", make_entry()))
    attach_fake_hass(flow)
    result = await flow.async_step_init()

    assert result["type"] == "form"
    data_schema = result["data_schema"]
    assert data_schema is not None
    schema = data_schema.schema
    selector_configs = [getattr(value, "config", {}) for value in schema.values()]
    assert all("selected_customers" not in str(key.schema) for key in schema)
    assert all("selected_customers" not in str(config) for config in selector_configs)
    defaults = [key.default() for key in schema if callable(key.default)]
    password_key = next(key for key in schema if key.schema == CONF_PASSWORD)
    assert not callable(password_key.default)
    assert str(DEFAULT_POLLING_INTERVAL_HOURS) in defaults
    assert DEFAULT_CO2_FACTOR_KG_PER_KWH in defaults
    co2_selector = next(
        value for key, value in schema.items() if key.schema == OPT_CO2_FACTOR_KG_PER_KWH
    )
    assert co2_selector.config["min"] == 0.001


def test_options_schema_uses_string_default_for_select_when_other_values_change() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnOptionsFlow

    entry = make_entry()
    entry.options = {OPT_POLLING_INTERVAL_HOURS: 6}
    flow = KepcoOnOptionsFlow(cast("Any", entry))

    validated = flow._schema()(
        {
            OPT_CO2_FACTOR_KG_PER_KWH: 0.5,
            OPT_HISTORY_MONTHS: 18,
        }
    )

    assert validated[OPT_POLLING_INTERVAL_HOURS] == "6"
    assert validated[OPT_CO2_FACTOR_KG_PER_KWH] == 0.5
    assert validated[OPT_HISTORY_MONTHS] == 18


@pytest.mark.asyncio
async def test_reauth_success_updates_existing_entry_and_keeps_configured_selection(
    patched_flow_dependencies: list[FakeSession],
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    entry.data[CONF_SAVE_PASSWORD] = True
    flow = KepcoOnConfigFlow()
    fake_hass = attach_fake_hass(flow)
    flow.handler = DOMAIN
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reauth_entry = Mock(return_value=entry)
    FakeClient.customer_results = [
        (
            customer(
                "key-3", apartment="새아파트", customer_number="CUST3", house_contract_number="H3"
            ),
            customer("key-1"),
        )
    ]

    result = await flow.async_step_reauth_confirm({CONF_PASSWORD: PASSWORD_SECRET})

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == PASSWORD_SECRET
    assert entry.data[CONF_SELECTED_CUSTOMERS] == ["key-1"]
    assert [item["stable_key"] for item in entry.data[CONF_CUSTOMERS]] == ["key-1"]
    assert entry.data[CONF_SESSION_HANDOFF]["cookies"] == []
    assert entry.title == "101동 1001호"
    assert fake_hass.config_entries.reloads == [entry.entry_id]
    assert patched_flow_dependencies[0].closed is True


@pytest.mark.asyncio
async def test_reauth_entry_step_shows_password_form_and_success_without_saving_password() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    entry.data[CONF_PASSWORD] = "OLD_PASSWORD"
    entry.data[CONF_SELECTED_CUSTOMERS] = ["key-2"]
    flow = KepcoOnConfigFlow()
    fake_hass = attach_fake_hass(flow)
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reauth_entry = Mock(return_value=entry)
    FakeClient.customer_results = [
        (
            customer(
                "key-2",
                dong="102",
                ho="1002",
                customer_number="CUST2",
                house_contract_number="HOUSE2",
            ),
        )
    ]

    shown = await flow.async_step_reauth({})
    result = await flow.async_step_reauth_confirm({CONF_PASSWORD: PASSWORD_SECRET})

    assert shown["type"] == "form"
    assert shown["step_id"] == "reauth_confirm"
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert CONF_PASSWORD not in entry.data
    assert entry.data[CONF_SELECTED_CUSTOMERS] == ["key-2"]
    assert [item["stable_key"] for item in entry.data[CONF_CUSTOMERS]] == ["key-2"]
    assert entry.title == "102동 1002호"
    assert fake_hass.config_entries.reloads == [entry.entry_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selected", "live"),
    [
        # The old flow silently switched to the first live customer here.
        (["key-1"], (customer("key-9", customer_number="CUST9", house_contract_number="H9"),)),
        # The old flow silently dropped key-2 and deleted its entities on reload.
        (["key-1", "key-2"], (customer("key-1"),)),
        (["stale"], (customer("key-1"),)),
        ([], (customer("key-1"),)),
    ],
    ids=["switch_to_other_customer", "drop_one_customer", "unknown_key", "empty_selection"],
)
async def test_reauth_aborts_without_changes_when_configured_customer_is_missing(
    patched_flow_dependencies: list[FakeSession],
    selected: list[str],
    live: tuple[KepcoCustomer, ...],
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    entry.data[CONF_SAVE_PASSWORD] = True
    entry.data[CONF_PASSWORD] = "OLD_PASSWORD"
    entry.data[CONF_SELECTED_CUSTOMERS] = selected
    before = json.loads(json.dumps(entry.data))
    flow = KepcoOnConfigFlow()
    fake_hass = attach_fake_hass(flow)
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reauth_entry = Mock(return_value=entry)
    FakeClient.customer_results = [live]

    result = await flow.async_step_reauth_confirm({CONF_PASSWORD: PASSWORD_SECRET})

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_customer_missing"
    assert entry.data == before
    assert fake_hass.config_entries.updates == []
    assert fake_hass.config_entries.reloads == []
    assert patched_flow_dependencies[0].closed is True


@pytest.mark.asyncio
async def test_reauth_keeps_stored_customer_key_when_live_key_drifts() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    flow = KepcoOnConfigFlow()
    fake_hass = attach_fake_hass(flow)
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reauth_entry = Mock(return_value=entry)
    # Same raw contract IDs as stored key-1, but a key derived from a changed member name.
    FakeClient.customer_results = [(customer("key-1-drifted", apartment="새이름아파트"),)]

    result = await flow.async_step_reauth_confirm({CONF_PASSWORD: PASSWORD_SECRET})

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_SELECTED_CUSTOMERS] == ["key-1"]
    stored = entry.data[CONF_CUSTOMERS]
    assert [item["stable_key"] for item in stored] == ["key-1"]
    assert stored[0]["apartment_name"] == "새이름아파트"
    assert stored[0][DATA_CUSTOMER_NUMBER] == RAW_CUSTOMER_SECRET
    assert "key-1-drifted" not in json.dumps(entry.data)
    assert fake_hass.config_entries.reloads == [entry.entry_id]


@pytest.mark.asyncio
async def test_reauth_failure_reshows_and_never_creates_entry(
    patched_flow_dependencies: list[FakeSession],
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    flow = KepcoOnConfigFlow()
    fake_hass = attach_fake_hass(flow)
    flow.handler = DOMAIN
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reauth_entry = Mock(return_value=entry)
    FakeAuth.login_results = [KepcoOnAuthError("bad")]

    result = await flow.async_step_reauth_confirm({CONF_PASSWORD: PASSWORD_SECRET})

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}
    assert fake_hass.config_entries.updates == []
    assert patched_flow_dependencies[0].closed is True


@pytest.mark.asyncio
async def test_reauth_unexpected_error_propagates_after_cleanup(
    patched_flow_dependencies: list[FakeSession],
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    flow = KepcoOnConfigFlow()
    fake_hass = attach_fake_hass(flow)
    flow.handler = DOMAIN
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reauth_entry = Mock(return_value=entry)
    FakeAuth.login_results = [RuntimeError("boom")]

    with pytest.raises(RuntimeError, match="boom"):
        await flow.async_step_reauth_confirm({CONF_PASSWORD: PASSWORD_SECRET})

    assert fake_hass.config_entries.updates == []
    assert patched_flow_dependencies[0].closed is True
    assert cast("Any", flow)._pending is None


@pytest.mark.asyncio
async def test_reauth_rejects_account_mismatch() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    flow = KepcoOnConfigFlow()
    fake_hass = attach_fake_hass(flow)
    flow.handler = DOMAIN
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reauth_entry = Mock(return_value=entry)
    FakeAuth.login_results = [account_session("OTHER_USER")]

    result = await flow.async_step_reauth_confirm({CONF_PASSWORD: PASSWORD_SECRET})

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}
    assert fake_hass.config_entries.updates == []


@pytest.mark.asyncio
async def test_reauth_requires_unique_id_match_before_legacy_data_hash() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    entry.unique_id = account_hash("SERVER_USER")
    entry.data[CONF_ACCOUNT_UID_HASH] = account_hash("OTHER_USER")
    flow = KepcoOnConfigFlow()
    fake_hass = attach_fake_hass(flow)
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reauth_entry = Mock(return_value=entry)
    FakeAuth.login_results = [account_session("OTHER_USER")]

    result = await flow.async_step_reauth_confirm({CONF_PASSWORD: PASSWORD_SECRET})

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}
    assert fake_hass.config_entries.updates == []


@pytest.mark.asyncio
async def test_reauth_uses_data_hash_only_for_legacy_missing_unique_id() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    entry.unique_id = None
    flow = KepcoOnConfigFlow()
    attach_fake_hass(flow)
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reauth_entry = Mock(return_value=entry)

    result = await flow.async_step_reauth_confirm({CONF_PASSWORD: PASSWORD_SECRET})

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"


@pytest.mark.asyncio
async def test_reauth_rejects_empty_refreshed_customers() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    flow = KepcoOnConfigFlow()
    fake_hass = attach_fake_hass(flow)
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reauth_entry = Mock(return_value=entry)
    FakeClient.customer_results = [()]

    result = await flow.async_step_reauth_confirm({CONF_PASSWORD: PASSWORD_SECRET})

    assert result["type"] == "form"
    assert result["errors"] == {"base": "no_customers"}
    assert fake_hass.config_entries.updates == []


@pytest.mark.asyncio
async def test_reconfigure_updates_customer_selection_only() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    flow = KepcoOnConfigFlow()
    fake_hass = attach_fake_hass(flow)
    flow.handler = DOMAIN
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reconfigure_entry = Mock(return_value=entry)

    shown = await flow.async_step_reconfigure()
    assert shown["type"] == "form"
    assert shown["step_id"] == "reconfigure"

    result = await flow.async_step_reconfigure({CONF_SELECTED_CUSTOMERS: ["key-2"]})

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SELECTED_CUSTOMERS] == ["key-2"]
    assert entry.unique_id == account_hash("SERVER_USER")
    assert entry.title == "102동 1002호"
    assert fake_hass.config_entries.reloads == [entry.entry_id]


@pytest.mark.asyncio
async def test_reconfigure_loaded_entry_uses_live_customers_and_persists_selected_only() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    entry.options = {OPT_POLLING_INTERVAL_HOURS: 12}
    live_client = FakeClient(cast("Any", object()))
    live_customers = (
        customer("key-1"),
        customer("key-3", apartment="새아파트"),
    )
    FakeClient.customer_results = [live_customers]
    entry.state = ConfigEntryState.LOADED
    entry.runtime_data = type("Runtime", (), {"client": live_client, "session": FakeSession()})()
    flow = KepcoOnConfigFlow()
    fake_hass = attach_fake_hass(flow)
    flow.handler = DOMAIN
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reconfigure_entry = Mock(return_value=entry)

    shown = await flow.async_step_reconfigure()
    result = await flow.async_step_reconfigure({CONF_SELECTED_CUSTOMERS: ["key-3"]})

    assert shown["type"] == "form"
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SELECTED_CUSTOMERS] == ["key-3"]
    assert [item["stable_key"] for item in entry.data[CONF_CUSTOMERS]] == ["key-3"]
    rendered = json.dumps(entry.data, ensure_ascii=False)
    assert "key-1" not in rendered
    assert RAW_CUSTOMER_SECRET in rendered
    assert "CUST2" not in rendered
    assert entry.options == {OPT_POLLING_INTERVAL_HOURS: 12}
    assert entry.title == "101동 1001호"
    assert fake_hass.config_entries.reloads == [entry.entry_id]


@pytest.mark.asyncio
async def test_reconfigure_live_customers_keep_stored_key_when_live_key_drifts() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    live_client = FakeClient(cast("Any", object()))
    FakeClient.customer_results = [
        (
            customer("key-1-drifted"),
            customer(
                "key-3", apartment="새아파트", customer_number="CUST3", house_contract_number="H3"
            ),
        )
    ]
    entry.state = ConfigEntryState.LOADED
    entry.runtime_data = type("Runtime", (), {"client": live_client, "session": FakeSession()})()
    flow = KepcoOnConfigFlow()
    fake_hass = attach_fake_hass(flow)
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reconfigure_entry = Mock(return_value=entry)

    shown = await flow.async_step_reconfigure()
    result = await flow.async_step_reconfigure({CONF_SELECTED_CUSTOMERS: ["key-1", "key-3"]})

    data_schema = shown["data_schema"]
    assert data_schema is not None
    options = str(next(iter(data_schema.schema.values())).config)
    assert "key-1-drifted" not in options
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SELECTED_CUSTOMERS] == ["key-1", "key-3"]
    assert [item["stable_key"] for item in entry.data[CONF_CUSTOMERS]] == ["key-1", "key-3"]
    assert fake_hass.config_entries.reloads == [entry.entry_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "error"),
    [
        (KepcoOnConnectionError(PASSWORD_SECRET), "cannot_connect"),
        (KepcoOnProtocolError(PASSWORD_SECRET), "protocol_changed_customers"),
    ],
)
async def test_reconfigure_live_refresh_error_allows_retry_with_safe_form_error(
    raised: Exception,
    error: str,
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    live_client = FakeClient(cast("Any", object()))
    refreshed = (customer("key-1"), customer("key-2", apartment="별빛아파트"))
    FakeClient.customer_results = [raised, refreshed]
    entry.state = ConfigEntryState.LOADED
    entry.runtime_data = type("Runtime", (), {"client": live_client, "session": FakeSession()})()
    flow = KepcoOnConfigFlow()
    attach_fake_hass(flow)
    flow.handler = DOMAIN
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reconfigure_entry = Mock(return_value=entry)

    failed = await flow.async_step_reconfigure()
    retried = await flow.async_step_reconfigure()
    result = await flow.async_step_reconfigure({CONF_SELECTED_CUSTOMERS: ["key-2"]})

    assert failed["type"] == "form"
    assert failed["errors"] == {"base": error}
    assert PASSWORD_SECRET not in repr(failed)
    assert retried["type"] == "form"
    assert result["type"] == "abort"
    assert entry.data[CONF_SELECTED_CUSTOMERS] == ["key-2"]


@pytest.mark.asyncio
async def test_reconfigure_live_refresh_unexpected_error_propagates() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    live_client = FakeClient(cast("Any", object()))
    FakeClient.customer_results = [RuntimeError(PASSWORD_SECRET)]
    entry.state = ConfigEntryState.LOADED
    entry.runtime_data = type("Runtime", (), {"client": live_client, "session": FakeSession()})()
    flow = KepcoOnConfigFlow()
    attach_fake_hass(flow)
    flow.handler = DOMAIN
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reconfigure_entry = Mock(return_value=entry)

    with pytest.raises(RuntimeError) as raised:
        await flow.async_step_reconfigure()

    assert str(raised.value) == PASSWORD_SECRET
    assert entry.data[CONF_SELECTED_CUSTOMERS] == ["key-1"]


@pytest.mark.asyncio
async def test_reconfigure_unloaded_entry_falls_back_to_stored_selected_customers() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    flow = KepcoOnConfigFlow()
    attach_fake_hass(flow)
    flow.handler = DOMAIN
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reconfigure_entry = Mock(return_value=entry)

    shown = await flow.async_step_reconfigure()
    result = await flow.async_step_reconfigure({CONF_SELECTED_CUSTOMERS: ["key-2"]})

    assert shown["type"] == "form"
    assert result["type"] == "abort"
    assert entry.data[CONF_SELECTED_CUSTOMERS] == ["key-2"]
    assert [item["stable_key"] for item in entry.data[CONF_CUSTOMERS]] == ["key-2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        ConfigEntryState.NOT_LOADED,
        ConfigEntryState.SETUP_ERROR,
        ConfigEntryState.SETUP_RETRY,
    ],
)
async def test_reconfigure_stale_runtime_state_falls_back_to_stored_customers(
    state: ConfigEntryState,
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    entry.state = state
    entry.runtime_data = type(
        "Runtime",
        (),
        {"client": FakeClient(cast("Any", object())), "session": FakeSession()},
    )()
    FakeClient.customer_results = [(customer("key-3", apartment="새아파트"),)]
    flow = KepcoOnConfigFlow()
    attach_fake_hass(flow)
    flow.handler = DOMAIN
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reconfigure_entry = Mock(return_value=entry)

    shown = await flow.async_step_reconfigure()
    result = await flow.async_step_reconfigure({CONF_SELECTED_CUSTOMERS: ["key-2"]})

    assert shown["type"] == "form"
    assert shown["errors"] is None
    assert result["type"] == "abort"
    assert entry.data[CONF_SELECTED_CUSTOMERS] == ["key-2"]
    assert len(FakeClient.customer_results) == 1


@pytest.mark.asyncio
async def test_reconfigure_closed_runtime_session_falls_back_to_stored_customers() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    entry.state = ConfigEntryState.LOADED
    session = FakeSession()
    session.closed = True
    entry.runtime_data = type(
        "Runtime",
        (),
        {"client": FakeClient(cast("Any", object())), "session": session},
    )()
    FakeClient.customer_results = [(customer("key-3", apartment="새아파트"),)]
    flow = KepcoOnConfigFlow()
    attach_fake_hass(flow)
    flow.handler = DOMAIN
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reconfigure_entry = Mock(return_value=entry)

    shown = await flow.async_step_reconfigure()
    result = await flow.async_step_reconfigure({CONF_SELECTED_CUSTOMERS: ["key-2"]})

    assert shown["type"] == "form"
    assert shown["errors"] is None
    assert result["type"] == "abort"
    assert entry.data[CONF_SELECTED_CUSTOMERS] == ["key-2"]
    assert len(FakeClient.customer_results) == 1


def _runtime(client: Any, *, last_exception: Exception | None = None) -> Any:
    coordinator = type("Coordinator", (), {"last_exception": last_exception})()
    return type(
        "Runtime",
        (),
        {"client": client, "session": FakeSession(), "coordinator": coordinator},
    )()


def _option_values(result: Mapping[str, Any]) -> list[str]:
    data_schema = result["data_schema"]
    assert data_schema is not None
    selector_config = next(iter(data_schema.schema.values())).config
    return [option["value"] for option in selector_config["options"]]


@pytest.mark.asyncio
async def test_reconfigure_after_reauth_customer_missing_offers_stored_customers_on_auth_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Following the reauth_customer_missing text must not dead-end on a loaded entry."""
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    entry.data[CONF_SAVE_PASSWORD] = True
    entry.data[CONF_PASSWORD] = "OLD_PASSWORD"
    FakeClient.customer_results = [
        # Reauth: the configured key-1 contract is no longer listed.
        (customer("key-9", customer_number="CUST9", house_contract_number="H9"),),
        # Reconfigure: the loaded runtime client still holds the rejected session.
        KepcoOnAuthError(PASSWORD_SECRET),
        KepcoOnAuthError(PASSWORD_SECRET),
    ]
    reauth = KepcoOnConfigFlow()
    attach_fake_hass(reauth)
    reauth.context = {"source": "reauth", "entry_id": entry.entry_id}
    reauth.flow_id = "flow-reauth"
    cast("Any", reauth)._get_reauth_entry = Mock(return_value=entry)
    aborted = await reauth.async_step_reauth_confirm({CONF_PASSWORD: PASSWORD_SECRET})
    assert aborted["reason"] == "reauth_customer_missing"

    entry.state = ConfigEntryState.LOADED
    entry.runtime_data = _runtime(FakeClient(cast("Any", object())))
    flow = KepcoOnConfigFlow()
    fake_hass = attach_fake_hass(flow)
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    flow.flow_id = "flow-reconfigure"
    cast("Any", flow)._get_reconfigure_entry = Mock(return_value=entry)

    shown = await flow.async_step_reconfigure()
    result = await flow.async_step_reconfigure({CONF_SELECTED_CUSTOMERS: ["key-2"]})

    assert shown["type"] == "form"
    assert shown["errors"] is None
    assert _option_values(shown) == ["key-1", "key-2"]
    # At most one live refresh fails; the submit reuses the stored list without a retry.
    assert len(FakeClient.customer_results) == 1
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SELECTED_CUSTOMERS] == ["key-2"]
    assert [item["stable_key"] for item in entry.data[CONF_CUSTOMERS]] == ["key-2"]
    assert fake_hass.config_entries.reloads == [entry.entry_id]
    assert PASSWORD_SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("last_exception", "expected_options", "live_calls"),
    [
        (ConfigEntryAuthFailed("expired"), ["key-1", "key-2"], 0),
        (UpdateFailed("temporary"), ["key-1", "key-3"], 1),
    ],
    ids=["auth_failed_uses_stored_customers", "other_failure_refreshes_live"],
)
async def test_reconfigure_skips_live_refresh_after_runtime_auth_failure(
    last_exception: Exception,
    expected_options: list[str],
    live_calls: int,
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    FakeClient.customer_results = [
        (
            customer("key-1"),
            customer(
                "key-3", apartment="새아파트", customer_number="CUST3", house_contract_number="H3"
            ),
        )
    ]
    entry.state = ConfigEntryState.LOADED
    entry.runtime_data = _runtime(FakeClient(cast("Any", object())), last_exception=last_exception)
    flow = KepcoOnConfigFlow()
    attach_fake_hass(flow)
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reconfigure_entry = Mock(return_value=entry)

    shown = await flow.async_step_reconfigure()
    result = await flow.async_step_reconfigure({CONF_SELECTED_CUSTOMERS: ["key-1"]})

    assert shown["type"] == "form"
    assert shown["errors"] is None
    assert _option_values(shown) == expected_options
    assert len(FakeClient.customer_results) == 1 - live_calls
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SELECTED_CUSTOMERS] == ["key-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("skips", "reason"),
    [
        (CustomerRowSkips(malformed=1), "protocol_changed_customers"),
        (CustomerRowSkips(unsupported=1, malformed=2), "protocol_changed_customers"),
        (CustomerRowSkips(unsupported=1), "reauth_customer_missing"),
    ],
    ids=["malformed", "malformed_and_unsupported", "unsupported_only"],
)
async def test_reauth_reports_protocol_change_when_malformed_rows_hide_configured_customer(
    patched_flow_dependencies: list[FakeSession],
    caplog: pytest.LogCaptureFixture,
    skips: CustomerRowSkips,
    reason: str,
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    caplog.set_level(logging.WARNING, logger="custom_components.kepco_on.config_flow")
    entry = make_entry()
    before = json.loads(json.dumps(entry.data))
    flow = KepcoOnConfigFlow()
    fake_hass = attach_fake_hass(flow)
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reauth_entry = Mock(return_value=entry)
    # The configured key-1 row was skipped by the parser; only another contract parsed.
    FakeClient.customer_results = [
        (customer("key-9", customer_number="CUST9", house_contract_number="H9"),)
    ]
    FakeClient.customer_row_skips = skips

    result = await flow.async_step_reauth_confirm({CONF_PASSWORD: PASSWORD_SECRET})

    assert result["type"] == "abort"
    assert result["reason"] == reason
    assert entry.data == before
    assert fake_hass.config_entries.updates == []
    assert fake_hass.config_entries.reloads == []
    assert patched_flow_dependencies[0].closed is True
    assert (
        f"({skips.unsupported} unsupported and {skips.malformed} malformed customer row(s) skipped)"
    ) in caplog.text
    for secret in (PASSWORD_SECRET, RAW_CUSTOMER_SECRET, RAW_HOUSE_SECRET):
        assert secret not in caplog.text


ROTATED_REFRESH_SECRET = "ROTATED_REFRESH_SECRET_CANARY"


def rotate_session_during_account_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make account-type validation rotate the session like a /sessionCheck recovery."""
    original = FakeClient.async_get_account_type

    async def rotating(self: FakeClient) -> str:
        current = self.auth.current_session
        assert current is not None
        self.auth.current_session = dataclasses.replace(
            current, refresh_token=ROTATED_REFRESH_SECRET
        )
        return await original(self)

    monkeypatch.setattr(FakeClient, "async_get_account_type", rotating)


@pytest.mark.asyncio
async def test_user_step_hands_off_session_rotated_during_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rotate_session_during_account_type(monkeypatch)
    flow = make_flow()

    result = await create_entry(flow)

    assert result["type"] == "create_entry"
    assert result["data"][CONF_ACCOUNT_UID_HASH] == account_hash("SERVER_USER")
    handoff = result["data"][CONF_SESSION_HANDOFF]
    assert handoff["refresh_token"] == ROTATED_REFRESH_SECRET
    assert TOKEN_SECRET not in json.dumps(handoff)


@pytest.mark.asyncio
async def test_reauth_hands_off_session_rotated_during_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    rotate_session_during_account_type(monkeypatch)
    entry = make_entry()
    flow = KepcoOnConfigFlow()
    attach_fake_hass(flow)
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reauth_entry = Mock(return_value=entry)

    result = await flow.async_step_reauth_confirm({CONF_PASSWORD: PASSWORD_SECRET})

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    handoff = entry.data[CONF_SESSION_HANDOFF]
    assert handoff["refresh_token"] == ROTATED_REFRESH_SECRET
    assert TOKEN_SECRET not in json.dumps(handoff)


@pytest.mark.asyncio
async def test_options_flow_completion_schedules_one_reload() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnOptionsFlow

    entry = make_entry()
    entry.options = {OPT_POLLING_INTERVAL_HOURS: 12}
    fake_hass = FakeHass()
    fake_hass.config_entries.entries_by_id[entry.entry_id] = entry
    flow = KepcoOnOptionsFlow(cast("Any", entry))
    flow.handler = entry.entry_id
    manager = OptionsFlowManager(cast("Any", fake_hass))

    result = await flow.async_step_init(
        {
            OPT_POLLING_INTERVAL_HOURS: "6",
            OPT_CO2_FACTOR_KG_PER_KWH: str(DEFAULT_CO2_FACTOR_KG_PER_KWH),
            OPT_HISTORY_MONTHS: 18,
        }
    )
    finished = await manager.async_finish_flow(cast("Any", flow), result)

    assert finished is result
    assert fake_hass.config_entries.reloads == [entry.entry_id]
    assert entry.update_listeners == []
    assert entry.options[OPT_POLLING_INTERVAL_HOURS] == 6


@pytest.mark.asyncio
async def test_reconfigure_rejects_empty_or_unknown_selection() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    entry = make_entry()
    flow = KepcoOnConfigFlow()
    attach_fake_hass(flow)
    flow.handler = DOMAIN
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    flow.flow_id = "flow-1"
    cast("Any", flow)._get_reconfigure_entry = Mock(return_value=entry)

    for selected in ([], ["missing"], ["key-1", "key-1"]):
        result = await flow.async_step_reconfigure({CONF_SELECTED_CUSTOMERS: selected})
        assert result["type"] == "form"
        assert result["errors"] == {"base": "invalid_selection"}


@pytest.mark.asyncio
async def test_reconfigure_aborts_when_stored_customers_are_empty_or_invalid() -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    invalid_customer = {
        "stable_key": "key-1",
        "apartment_name": "푸른아파트",
        "dong": "101",
        "ho": "1001",
        "contract_method": "아파트(단일계약)",
        "is_supported": "False",
        DATA_CUSTOMER_NUMBER: RAW_CUSTOMER_SECRET,
        DATA_HOUSE_CONTRACT_NUMBER: RAW_HOUSE_SECRET,
    }
    empty_string_customer = {
        **invalid_customer,
        "is_supported": True,
        DATA_CUSTOMER_NUMBER: "",
    }
    for stored_customers in (
        [],
        [{"stable_key": "key-1"}],
        [invalid_customer],
        [empty_string_customer],
    ):
        entry = make_entry()
        entry.data[CONF_CUSTOMERS] = stored_customers
        flow = KepcoOnConfigFlow()
        attach_fake_hass(flow)
        flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
        flow.flow_id = "flow-1"
        cast("Any", flow)._get_reconfigure_entry = Mock(return_value=entry)

        result = await flow.async_step_reconfigure()

        assert result["type"] == "abort"
        assert result["reason"] == "no_customers"


def test_translation_files_have_required_key_parity() -> None:
    required_errors = {
        "invalid_auth",
        "cannot_connect",
        "rate_limited",
        "unsupported_account",
        "no_customers",
        "mfa_required",
        "protocol_changed",
        "protocol_changed_login",
        "protocol_changed_account_type",
        "protocol_changed_customers",
        "invalid_selection",
        "invalid_co2_factor",
        "invalid_history_months",
        "unknown",
    }
    required_aborts = {
        "already_configured",
        "no_customers",
        "protocol_changed_customers",
        "reauth_customer_missing",
        "reauth_successful",
        "reconfigure_successful",
    }
    files = [
        ROOT / "custom_components/kepco_on/strings.json",
        ROOT / "custom_components/kepco_on/translations/en.json",
        ROOT / "custom_components/kepco_on/translations/ko.json",
    ]
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in files]

    assert payloads[0] == payloads[1]
    for payload in payloads:
        assert set(payload["config"]["error"]) >= required_errors
        assert set(payload["config"]["abort"]) >= required_aborts
        assert set(payload["options"]["error"]) >= {
            "invalid_selection",
            "invalid_co2_factor",
            "invalid_history_months",
        }
        warning = payload["config"]["step"]["user"]["description"]
        assert "not encrypted secret vaults" in warning or "암호화된 비밀 금고" in warning
