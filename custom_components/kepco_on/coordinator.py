"""Data coordinator for KEPCO ON selected customers."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import KepcoOnClient
from .const import DEFAULT_POLLING_INTERVAL_HOURS, DOMAIN, OPT_POLLING_INTERVAL_HOURS
from .exceptions import (
    KepcoOnAuthError,
    KepcoOnConnectionError,
    KepcoOnError,
    KepcoOnNoCustomersError,
    KepcoOnProtocolError,
    KepcoOnRateLimitError,
    KepcoOnSessionExpired,
)
from .models import KepcoBill, KepcoCoordinatorData, KepcoCustomer
from .repairs import async_clear_issue, async_create_issue

_LOGGER = logging.getLogger(__name__)


def _update_interval(entry: ConfigEntry) -> timedelta:
    """Return the configured polling interval."""
    try:
        hours = int(entry.options.get(OPT_POLLING_INTERVAL_HOURS, DEFAULT_POLLING_INTERVAL_HOURS))
    except TypeError, ValueError:
        hours = DEFAULT_POLLING_INTERVAL_HOURS
    return timedelta(hours=hours)


def _safe_customer_error(err: Exception) -> str:
    """Map a per-customer exception to a non-sensitive status code."""
    if isinstance(err, KepcoOnNoCustomersError):
        return "no_customers"
    if isinstance(err, KepcoOnProtocolError):
        return "protocol_error"
    if isinstance(err, KepcoOnError):
        return "api_error"
    return "unknown_error"


class KepcoOnDataUpdateCoordinator(DataUpdateCoordinator[KepcoCoordinatorData]):
    """Poll latest bills for selected KEPCO ON customers."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: KepcoOnClient,
        customers: tuple[KepcoCustomer, ...],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=_update_interval(entry),
            always_update=False,
        )
        self.client = client
        self.customers = customers
        self.entry = entry
        self._normal_update_interval = _update_interval(entry)
        self._temporary_failures = 0

    def _retry_interval(self, rate_limited: bool, retry_after: float) -> float:
        """Back off transient failures without waiting for the full billing interval."""
        self._temporary_failures = min(self._temporary_failures + 1, 6)
        multiplier = float(2 ** (self._temporary_failures - 1))
        seconds = (
            max(min(20 * 60.0 * multiplier, 60 * 60.0), retry_after)
            if rate_limited
            else min(60.0 * multiplier, 15 * 60.0)
        )
        self.update_interval = timedelta(seconds=seconds)
        return seconds

    async def _async_update_data(self) -> KepcoCoordinatorData:
        """Fetch each selected customer's latest bill sequentially."""
        bills: dict[str, KepcoBill] = {}
        errors: dict[str, str] = {}
        protocol_error_seen = False
        temporary_error_seen = False
        rate_limited = False
        retry_after = 0.0
        for customer in self.customers:
            if rate_limited:
                errors[customer.stable_key] = "rate_limited"
                continue
            try:
                bills[customer.stable_key] = await self.client.async_get_bill(customer)
            except (KepcoOnSessionExpired, KepcoOnAuthError) as err:
                del err
                raise ConfigEntryAuthFailed("KEPCO ON authentication expired") from None
            except KepcoOnRateLimitError as err:
                temporary_error_seen = rate_limited = True
                retry_after = err.retry_after or 0.0
                errors[customer.stable_key] = "rate_limited"
            except KepcoOnConnectionError:
                temporary_error_seen = True
                errors[customer.stable_key] = "connection_error"
            except KepcoOnProtocolError as err:
                protocol_error_seen = True
                async_create_issue(self.hass, self.entry, "bill_schema_changed")
                errors[customer.stable_key] = _safe_customer_error(err)
            except KepcoOnError as err:
                errors[customer.stable_key] = _safe_customer_error(err)

        if temporary_error_seen:
            delay = self._retry_interval(rate_limited, retry_after)
            if not bills:
                reason = "rate limited" if rate_limited else "temporary connection failure"
                raise UpdateFailed(
                    f"KEPCO ON {reason}; retry in {delay:g}s", retry_after=delay
                ) from None
        else:
            self._temporary_failures = 0
            self.update_interval = self._normal_update_interval
        if not bills:
            raise UpdateFailed("No KEPCO ON selected customers updated")
        if not protocol_error_seen:
            async_clear_issue(self.hass, self.entry, "bill_schema_changed")

        return KepcoCoordinatorData(
            customers=self.customers,
            bills_by_customer_key=bills,
            errors_by_customer_key=errors,
            last_success=datetime.now(UTC),
        )


__all__ = ["KepcoOnDataUpdateCoordinator"]
