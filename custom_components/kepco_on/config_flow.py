"""Config flow for the KEPCO ON integration."""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import voluptuous as vol
from aiohttp import CookieJar
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import KepcoOnClient
from .auth import KepcoOnAuth
from .const import (
    CONF_ACCOUNT_UID_HASH,
    CONF_CUSTOMERS,
    CONF_DISPLAY_NAME,
    CONF_SAVE_PASSWORD,
    CONF_SELECTED_CUSTOMERS,
    CONF_SESSION_HANDOFF,
    CONF_USERNAME,
    CONFIG_ENTRY_VERSION,
    DEFAULT_CO2_FACTOR_KG_PER_KWH,
    DEFAULT_POLLING_INTERVAL_HOURS,
    DOMAIN,
    OPT_CO2_FACTOR_KG_PER_KWH,
    OPT_ENABLE_POWER_PLANNER,
    OPT_HISTORY_MONTHS,
    OPT_POLLING_INTERVAL_HOURS,
    OPT_POWER_PLANNER_CUSTOMER_ID,
    POLLING_INTERVAL_HOURS,
)
from .exceptions import (
    KepcoOnAuthError,
    KepcoOnConnectionError,
    KepcoOnError,
    KepcoOnMfaRequired,
    KepcoOnNoCustomersError,
    KepcoOnProtocolError,
    KepcoOnRateLimitError,
    KepcoOnUnsupportedAccount,
)
from .models import (
    KepcoAccountSession,
    KepcoCustomer,
    customer_location_name,
    selected_customer_location_title,
    selected_customers,
    serialize_customer,
    stored_customers,
    strict_selected_stored_customers,
    validate_selected_keys,
)
from .power_planner import ORIGIN, PowerPlannerClient, PowerPlannerSessionStore
from .session_store import session_to_payload

DEFAULT_HISTORY_MONTHS = 12
MIN_CO2_FACTOR = 0.001

_LOGGER = logging.getLogger(__name__)


class FlowSessionStore:
    """In-memory store for one config-flow login attempt."""

    def __init__(self) -> None:
        self.session: KepcoAccountSession | None = None

    async def async_load(self) -> KepcoAccountSession | None:
        """Return the captured session."""
        return self.session

    async def async_save(self, session: KepcoAccountSession) -> None:
        """Capture a session without writing persistent storage."""
        self.session = session

    async def async_clear(self) -> None:
        """Clear the captured session."""
        self.session = None


@dataclass(slots=True)
class PendingConfig:
    """State held between user and customer steps."""

    username: str
    password: str
    save_password: bool
    display_name: str
    account_uid_hash: str
    customers: tuple[KepcoCustomer, ...]
    session: KepcoAccountSession
    client_session: Any


def _account_uid_hash(user_id: str) -> str:
    return hashlib.sha256(f"kepco_on:{user_id.strip()}".encode()).hexdigest()


def _customer_label(customer: KepcoCustomer) -> str:
    if customer.is_house:
        return f"{customer.apartment_name} ({customer.customer_number[:2]}-****)"
    return f"{customer.apartment_name} {customer_location_name(customer)}"


def _config_entry_title(entry_data: Mapping[str, Any], customers: Sequence[KepcoCustomer]) -> str:
    """Return an explicit display name or the normalized selected location."""
    display_name = entry_data.get(CONF_DISPLAY_NAME)
    if isinstance(display_name, str) and (normalized := display_name.strip()):
        return normalized
    return selected_customer_location_title(customers)


def _customer_options(customers: tuple[KepcoCustomer, ...]) -> list[selector.SelectOptionDict]:
    return [
        selector.SelectOptionDict(value=customer.stable_key, label=_customer_label(customer))
        for customer in customers
    ]


def _with_stored_customer_keys(
    customers: Sequence[KepcoCustomer],
    entry_data: Mapping[str, Any],
) -> tuple[KepcoCustomer, ...]:
    """Reuse stored stable keys for live customers whose raw contract IDs are unchanged.

    Customer keys hash the account user ID and member name, so a member-name change
    at KEPCO gives the same contract a new key. Keeping the stored key keeps the
    configured selection and every existing entity unique ID.
    """
    stored = stored_customers(entry_data) or ()
    stored_keys = {record.stable_key for record in stored}
    live_keys = {customer.stable_key for customer in customers}
    drifted: dict[tuple[str, str], str] = {}
    for record in stored:
        if record.stable_key not in live_keys:
            drifted.setdefault(
                (record.customer_number, record.house_contract_number), record.stable_key
            )
    result: list[KepcoCustomer] = []
    for customer in customers:
        if customer.stable_key not in stored_keys:
            stored_key = drifted.pop(
                (customer.customer_number, customer.house_contract_number), None
            )
            if stored_key is not None:
                customer = dataclasses.replace(customer, stable_key=stored_key)
        result.append(customer)
    return tuple(result)


def _customer_schema(
    customers: tuple[KepcoCustomer, ...],
    selected: list[str] | None = None,
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_SELECTED_CUSTOMERS, default=selected or []): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_customer_options(customers),
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        }
    )


def _base_user_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME): selector.TextSelector(selector.TextSelectorConfig()),
            vol.Required(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(CONF_SAVE_PASSWORD, default=False): bool,
            vol.Optional(CONF_DISPLAY_NAME, default=""): selector.TextSelector(
                selector.TextSelectorConfig()
            ),
        }
    )


PROTOCOL_STAGE_ERRORS = {
    "login": "protocol_changed_login",
    "account_type": "protocol_changed_account_type",
    "customers": "protocol_changed_customers",
}


def _map_error(err: Exception, *, stage: str | None = None) -> str:
    if isinstance(err, KepcoOnMfaRequired):
        return "mfa_required"
    if isinstance(err, KepcoOnAuthError):
        return "invalid_auth"
    if isinstance(err, KepcoOnRateLimitError):
        return "rate_limited"
    if isinstance(err, KepcoOnConnectionError):
        return "cannot_connect"
    if isinstance(err, KepcoOnUnsupportedAccount):
        return "unsupported_account"
    if isinstance(err, KepcoOnNoCustomersError):
        return "no_customers"
    if isinstance(err, KepcoOnProtocolError):
        if stage is None:
            return "protocol_changed"
        return PROTOCOL_STAGE_ERRORS.get(stage, "protocol_changed")
    return "unknown"


EXPECTED_CONFIG_FLOW_ERRORS = (
    KepcoOnAuthError,
    KepcoOnConnectionError,
    KepcoOnMfaRequired,
    KepcoOnNoCustomersError,
    KepcoOnProtocolError,
    KepcoOnRateLimitError,
    KepcoOnUnsupportedAccount,
)


def _can_use_live_reconfigure_client(entry: config_entries.ConfigEntry) -> bool:
    """Return whether the loaded runtime client is usable for reconfigure refresh."""
    if entry.state is not ConfigEntryState.LOADED:
        return False
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        return False
    session = getattr(runtime_data, "session", None)
    return not bool(getattr(session, "closed", True))


def _runtime_auth_failed(entry: config_entries.ConfigEntry) -> bool:
    """Return whether the runtime coordinator stopped after an authentication failure."""
    coordinator = getattr(getattr(entry, "runtime_data", None), "coordinator", None)
    return isinstance(getattr(coordinator, "last_exception", None), ConfigEntryAuthFailed)


async def _close_session(client_session: Any | None) -> None:
    """Detach a flow session from Home Assistant's shared connector."""
    if client_session is not None and not getattr(client_session, "closed", False):
        try:
            client_session.detach()
        except Exception:
            _LOGGER.warning("Failed to close KEPCO ON config flow session", exc_info=True)


class KepcoOnConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle KEPCO ON config, reauth, and reconfigure flows."""

    VERSION = CONFIG_ENTRY_VERSION

    def __init__(self) -> None:
        self._pending: PendingConfig | None = None
        self._reconfigure_customers: tuple[KepcoCustomer, ...] | None = None

    @callback
    def async_remove(self) -> None:
        """Close any pending dedicated login session when the flow is abandoned."""
        pending = self._pending
        self._pending = None
        if pending is not None:
            self.hass.async_create_task(_close_session(pending.client_session))
        super().async_remove()

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow."""
        return KepcoOnOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial user credential step."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_base_user_schema())

        await _close_session(self._pending.client_session if self._pending else None)
        self._pending = None
        client_session = async_create_clientsession(
            self.hass,
            auto_cleanup=False,
            cookie_jar=CookieJar(),
        )
        abort_reason: str | None = None
        form_error: str | None = None
        login_succeeded = False
        validation_stage = "login"
        try:
            store = FlowSessionStore()
            auth = KepcoOnAuth(client_session, store=store)
            session = await auth.async_login(user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            client = KepcoOnClient(auth)
            validation_stage = "account_type"
            await client.async_get_account_type()
            validation_stage = "customers"
            customers = await client.async_get_customers()
            if not customers:
                raise KepcoOnNoCustomersError("No KEPCO ON apartment customers found")
            validation_stage = "unique_id"
            account_uid_hash = _account_uid_hash(session.user_id)
            await self.async_set_unique_id(account_uid_hash)
            self._abort_if_unique_id_configured()
            # Hand off the session as it is now; a /sessionCheck recovery during the
            # validation requests above may have rotated its tokens and cookies.
            handoff_session = await auth.async_export_session_snapshot()
            login_succeeded = True
        except AbortFlow as err:
            abort_reason = err.reason
        except EXPECTED_CONFIG_FLOW_ERRORS as err:
            _LOGGER.log(
                logging.ERROR
                if isinstance(err, KepcoOnConnectionError | KepcoOnProtocolError)
                else logging.WARNING,
                "KEPCO ON config flow failed during %s (%s): %s",
                validation_stage,
                type(err).__name__,
                err,
            )
            form_error = _map_error(err, stage=validation_stage)
        finally:
            if not login_succeeded:
                await _close_session(client_session)
        if abort_reason is not None:
            return self.async_abort(reason=abort_reason)
        if form_error is not None:
            return self.async_show_form(
                step_id="user",
                data_schema=_base_user_schema(),
                errors={"base": form_error},
            )

        self._pending = PendingConfig(
            username=str(user_input[CONF_USERNAME]).strip(),
            password=str(user_input[CONF_PASSWORD]),
            save_password=bool(user_input.get(CONF_SAVE_PASSWORD, False)),
            display_name=str(user_input.get(CONF_DISPLAY_NAME) or "").strip(),
            account_uid_hash=account_uid_hash,
            customers=tuple(customers),
            session=handoff_session,
            client_session=client_session,
        )
        return self.async_show_form(
            step_id="customer",
            data_schema=_customer_schema(tuple(customers), [customers[0].stable_key]),
        )

    async def async_step_customer(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Select one or more customers after authentication."""
        if self._pending is None:
            return await self.async_step_user()
        available = {customer.stable_key for customer in self._pending.customers}
        if user_input is None:
            return self.async_show_form(
                step_id="customer",
                data_schema=_customer_schema(self._pending.customers),
            )
        selected = validate_selected_keys(user_input.get(CONF_SELECTED_CUSTOMERS), available)
        if selected is None:
            return self.async_show_form(
                step_id="customer",
                data_schema=_customer_schema(self._pending.customers),
                errors={"base": "invalid_selection"},
            )

        pending = self._pending
        selected_records = selected_customers(pending.customers, selected)
        data: dict[str, Any] = {
            CONF_USERNAME: pending.username,
            CONF_SAVE_PASSWORD: pending.save_password,
            CONF_ACCOUNT_UID_HASH: pending.account_uid_hash,
            CONF_CUSTOMERS: [serialize_customer(customer) for customer in selected_records],
            CONF_SELECTED_CUSTOMERS: selected,
            CONF_SESSION_HANDOFF: session_to_payload(pending.session),
        }
        if pending.display_name:
            data[CONF_DISPLAY_NAME] = pending.display_name
        if pending.save_password:
            data[CONF_PASSWORD] = pending.password
        title = _config_entry_title(data, selected_records)
        await _close_session(pending.client_session)
        self._pending = None
        return self.async_create_entry(title=title, data=data)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Start reauthentication."""
        del entry_data
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Confirm credentials for an existing config entry."""
        entry = self._get_reauth_entry()
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_PASSWORD): selector.TextSelector(
                            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                        )
                    }
                ),
            )
        client_session = async_create_clientsession(
            self.hass,
            auto_cleanup=False,
            cookie_jar=CookieJar(),
        )
        form_error: str | None = None
        login_succeeded = False
        validation_stage = "login"
        try:
            store = FlowSessionStore()
            username = str(entry.data[CONF_USERNAME])
            password = str(user_input[CONF_PASSWORD])
            auth = KepcoOnAuth(
                client_session,
                store=store,
                reauth_username=username,
                reauth_password=password,
            )
            session = await auth.async_login(username, password)
            account_uid_hash = _account_uid_hash(session.user_id)
            expected_account_uid_hash = entry.unique_id or entry.data.get(CONF_ACCOUNT_UID_HASH)
            if account_uid_hash != expected_account_uid_hash:
                raise KepcoOnAuthError("Authenticated KEPCO ON account does not match entry")
            client = KepcoOnClient(auth)
            validation_stage = "account_type"
            await client.async_get_account_type()
            validation_stage = "customers"
            customers = await client.async_get_customers()
            if not customers:
                raise KepcoOnNoCustomersError("No KEPCO ON apartment customers found")
            row_skips = client.customer_row_skips
            handoff_session = await auth.async_export_session_snapshot()
            login_succeeded = True
        except EXPECTED_CONFIG_FLOW_ERRORS as err:
            form_error = _map_error(err, stage=validation_stage)
        finally:
            if not login_succeeded:
                await _close_session(client_session)
        if form_error is not None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_PASSWORD): selector.TextSelector(
                            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                        )
                    }
                ),
                errors={"base": form_error},
            )

        # Reauthentication renews credentials only. It must never switch or drop the
        # configured customers; a reload would delete the entities of a dropped one.
        customers = _with_stored_customer_keys(customers, entry.data)
        live_keys = {customer.stable_key for customer in customers}
        current = [str(value) for value in entry.data.get(CONF_SELECTED_CUSTOMERS, [])]
        missing = [key for key in current if key not in live_keys]
        if not current or missing:
            # A configured contract hidden by a skipped malformed row is a KEPCO response
            # change, not a removed customer; do not steer the user to delete it.
            reason = (
                "protocol_changed_customers"
                if missing and row_skips.malformed
                else "reauth_customer_missing"
            )
            _LOGGER.warning(
                "KEPCO ON reauthentication stopped: %d of %d configured customer(s) "
                "were not found in the account (%d unsupported and %d malformed "
                "customer row(s) skipped); the entry was not changed",
                len(missing),
                len(current),
                row_skips.unsupported,
                row_skips.malformed,
            )
            await _close_session(client_session)
            return self.async_abort(reason=reason)
        selected_records = selected_customers(customers, current)
        data = dict(entry.data)
        data[CONF_ACCOUNT_UID_HASH] = account_uid_hash
        data[CONF_CUSTOMERS] = [serialize_customer(customer) for customer in selected_records]
        data[CONF_SELECTED_CUSTOMERS] = current
        data[CONF_SESSION_HANDOFF] = session_to_payload(handoff_session)
        if data.get(CONF_SAVE_PASSWORD):
            data[CONF_PASSWORD] = str(user_input[CONF_PASSWORD])
        else:
            data.pop(CONF_PASSWORD, None)
        await _close_session(client_session)
        return self.async_update_reload_and_abort(
            entry,
            title=_config_entry_title(data, selected_records),
            data=data,
            reason="reauth_successful",
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Update customer selection for an existing entry."""
        entry = self._get_reconfigure_entry()
        customers, refresh_error = await self._async_reconfigure_customers(entry)
        if customers is None:
            if refresh_error is not None:
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=_customer_schema(()),
                    errors={"base": refresh_error},
                )
            return self.async_abort(reason="no_customers")
        current = [str(value) for value in entry.data.get(CONF_SELECTED_CUSTOMERS, [])]
        if user_input is None:
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=_customer_schema(customers, current),
                errors={"base": refresh_error} if refresh_error is not None else None,
            )
        selected = validate_selected_keys(
            user_input.get(CONF_SELECTED_CUSTOMERS),
            {customer.stable_key for customer in customers},
        )
        if selected is None:
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=_customer_schema(customers, current),
                errors={"base": "invalid_selection"},
            )
        selected_records = selected_customers(customers, selected)
        data = dict(entry.data)
        data[CONF_CUSTOMERS] = [serialize_customer(customer) for customer in selected_records]
        data[CONF_SELECTED_CUSTOMERS] = selected
        return self.async_update_reload_and_abort(
            entry,
            title=_config_entry_title(data, selected_records),
            data=data,
            reason="reconfigure_successful",
        )

    async def _async_reconfigure_customers(
        self,
        entry: config_entries.ConfigEntry,
    ) -> tuple[tuple[KepcoCustomer, ...] | None, str | None]:
        """Return live customers when runtime is loaded, otherwise stored customers."""
        if self._reconfigure_customers is not None:
            return self._reconfigure_customers, None

        # After an auth failure the runtime session is known to be rejected; another
        # request through it would only repeat a login with a stale saved password.
        if _can_use_live_reconfigure_client(entry) and not _runtime_auth_failed(entry):
            runtime_data = entry.runtime_data
            client = getattr(runtime_data, "client", None)
            if client is None:
                fallback_customers: tuple[KepcoCustomer, ...] | None = stored_customers(entry.data)
                self._reconfigure_customers = fallback_customers
                return fallback_customers, None
            try:
                live_customers: tuple[KepcoCustomer, ...] = _with_stored_customer_keys(
                    await client.async_get_customers(), entry.data
                )
            except KepcoOnAuthError:
                # Offer the stored selection so a customer that reauthentication could
                # not find can still be deselected; the reload then asks for reauth.
                _LOGGER.debug(
                    "KEPCO ON reconfigure uses stored customers after an authentication error"
                )
                fallback_customers = stored_customers(entry.data)
                self._reconfigure_customers = fallback_customers
                return fallback_customers, None
            except (
                KepcoOnConnectionError,
                KepcoOnNoCustomersError,
                KepcoOnProtocolError,
                KepcoOnRateLimitError,
                KepcoOnUnsupportedAccount,
            ) as err:
                return None, _map_error(err, stage="customers")
            if not live_customers:
                return None, "no_customers"
            self._reconfigure_customers = live_customers
            return live_customers, None

        fallback_customers = stored_customers(entry.data)
        self._reconfigure_customers = fallback_customers
        return fallback_customers, None


class KepcoOnOptionsFlow(config_entries.OptionsFlowWithReload):
    """Handle KEPCO ON options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._planner_customer_key: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage integration options."""
        if user_input is None:
            return self.async_show_form(step_id="init", data_schema=self._schema())

        options, error = self._validate_options(user_input)
        if error is not None:
            return self.async_show_form(
                step_id="init",
                data_schema=self._schema(),
                errors={"base": error},
            )
        enabled = bool(options.get(OPT_ENABLE_POWER_PLANNER, False))
        was_enabled = bool(self._config_entry.options.get(OPT_ENABLE_POWER_PLANNER, False))
        if enabled and (not was_enabled or user_input.get(CONF_PASSWORD)):
            error = await self._async_prepare_planner(user_input.get(CONF_PASSWORD))
            if error is not None:
                return self.async_show_form(
                    step_id="init", data_schema=self._schema(), errors={"base": error}
                )
            options[OPT_POWER_PLANNER_CUSTOMER_ID] = self._planner_customer_key
        elif was_enabled and not enabled:
            try:
                await PowerPlannerSessionStore(self.hass, self._config_entry.entry_id).async_clear()
            except OSError:
                return self.async_show_form(
                    step_id="init", data_schema=self._schema(), errors={"base": "unknown"}
                )
        return self.async_create_entry(title=None, data=options)

    async def _async_prepare_planner(self, supplied_password: object) -> str | None:
        """Validate selected ownership and persist a cookie, never an option password."""
        entry = self._config_entry
        password = supplied_password or entry.data.get(CONF_PASSWORD)
        if password is not None and not isinstance(password, str):
            return "invalid_auth"
        session = async_create_clientsession(
            self.hass, auto_cleanup=False, cookie_jar=CookieJar(quote_cookie=False)
        )
        store = PowerPlannerSessionStore(self.hass, entry.entry_id)
        try:
            client = PowerPlannerClient(session)
            if password:
                await client.async_login(str(entry.data[CONF_USERNAME]), password)
            else:
                session_id = await store.async_load()
                if session_id is None:
                    return "power_planner_password_required"
                session.cookie_jar.update_cookies({"JSESSIONID": session_id}, response_url=ORIGIN)
                # The first read also verifies that the restored cookie remains valid.
            customers = strict_selected_stored_customers(entry.data)
            await client.async_validate_customers(
                {customer.house_contract_number for customer in customers}
            )
            matches = [
                customer.stable_key
                for customer in customers
                if customer.house_contract_number == client.main_customer_number
            ]
            if len(matches) != 1:
                return "invalid_selection"
            cookie = session.cookie_jar.filter_cookies(ORIGIN).get("JSESSIONID")
            if cookie is None:
                return "invalid_auth"
            await store.async_save(cookie.value)
            self._planner_customer_key = matches[0]
        except KepcoOnError as error:
            return _map_error(error)
        except OSError, ValueError:
            return "unknown"
        finally:
            await _close_session(session)
        return None

    def _schema(self) -> vol.Schema:
        options = self._config_entry.options
        return vol.Schema(
            {
                vol.Optional(
                    OPT_ENABLE_POWER_PLANNER, default=options.get(OPT_ENABLE_POWER_PLANNER, False)
                ): bool,
                vol.Optional(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(
                    OPT_POLLING_INTERVAL_HOURS,
                    default=str(
                        options.get(
                            OPT_POLLING_INTERVAL_HOURS,
                            DEFAULT_POLLING_INTERVAL_HOURS,
                        )
                    ),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[str(value) for value in POLLING_INTERVAL_HOURS],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    OPT_CO2_FACTOR_KG_PER_KWH,
                    default=options.get(OPT_CO2_FACTOR_KG_PER_KWH, DEFAULT_CO2_FACTOR_KG_PER_KWH),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_CO2_FACTOR,
                        max=10,
                        step="any",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    OPT_HISTORY_MONTHS,
                    default=options.get(OPT_HISTORY_MONTHS, DEFAULT_HISTORY_MONTHS),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=24,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )

    def _validate_options(
        self,
        user_input: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        try:
            interval = int(
                user_input.get(OPT_POLLING_INTERVAL_HOURS, DEFAULT_POLLING_INTERVAL_HOURS)
            )
        except TypeError, ValueError:
            return {}, "unknown"
        if interval not in POLLING_INTERVAL_HOURS:
            return {}, "unknown"
        try:
            co2_factor = float(
                Decimal(
                    str(user_input.get(OPT_CO2_FACTOR_KG_PER_KWH, DEFAULT_CO2_FACTOR_KG_PER_KWH))
                )
            )
        except InvalidOperation, ValueError:
            return {}, "invalid_co2_factor"
        if not math.isfinite(co2_factor) or co2_factor <= 0 or co2_factor > 10:
            return {}, "invalid_co2_factor"
        try:
            history_months = int(user_input.get(OPT_HISTORY_MONTHS, DEFAULT_HISTORY_MONTHS))
        except TypeError, ValueError:
            return {}, "invalid_history_months"
        if not 1 <= history_months <= 24:
            return {}, "invalid_history_months"
        enabled = user_input.get(
            OPT_ENABLE_POWER_PLANNER,
            self._config_entry.options.get(OPT_ENABLE_POWER_PLANNER, False),
        )
        if not isinstance(enabled, bool):
            return {}, "unknown"
        planner_option = (
            {OPT_ENABLE_POWER_PLANNER: enabled}
            if OPT_ENABLE_POWER_PLANNER in user_input
            or OPT_ENABLE_POWER_PLANNER in self._config_entry.options
            else {}
        )
        if enabled and OPT_POWER_PLANNER_CUSTOMER_ID in self._config_entry.options:
            planner_option[OPT_POWER_PLANNER_CUSTOMER_ID] = self._config_entry.options[
                OPT_POWER_PLANNER_CUSTOMER_ID
            ]
        return (
            {
                OPT_POLLING_INTERVAL_HOURS: interval,
                OPT_CO2_FACTOR_KG_PER_KWH: co2_factor,
                OPT_HISTORY_MONTHS: history_months,
                **planner_option,
            },
            None,
        )


__all__ = ["KepcoOnConfigFlow", "KepcoOnOptionsFlow"]
