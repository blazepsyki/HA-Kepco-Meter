"""Refresh the optional planner independently from monthly billing."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_USERNAME, DOMAIN
from .exceptions import (
    KepcoOnAuthError,
    KepcoOnConnectionError,
    KepcoOnNoCustomersError,
    KepcoOnProtocolError,
    KepcoOnRateLimitError,
    KepcoOnUnsupportedAccount,
)
from .models import KepcoCustomer, PowerPlannerData, PowerPlannerSnapshot
from .power_planner import PowerPlannerClient
from .repairs import async_clear_issue, async_create_issue

_LOGGER = logging.getLogger(__name__)


class PowerPlannerCoordinator(DataUpdateCoordinator[PowerPlannerData]):
    """Keep the session alive every 20 minutes and fetch quantities hourly."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: PowerPlannerClient,
        customers: tuple[KepcoCustomer, ...],
        password: str | None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_power_planner",
            update_interval=timedelta(minutes=20),
            always_update=False,
        )
        self.client = client
        self.customers = customers
        self.entry = entry
        self._password = password
        self._last_fetch: datetime | None = None

    def _empty(self, status: str) -> PowerPlannerData:
        return PowerPlannerData(
            {
                customer.stable_key: PowerPlannerSnapshot(status=status)
                for customer in self.customers
            }
        )

    async def _async_update_data(self) -> PowerPlannerData:
        now = datetime.now(UTC)
        fetch = self._last_fetch is None or now - self._last_fetch >= timedelta(hours=1)
        try:
            if not self.customers:
                raise KepcoOnNoCustomersError("Power Planner bound customer is not selected")
            snapshot = await self.client.async_poll(
                str(self.entry.data[CONF_USERNAME]),
                self._password,
                {customer.house_contract_number for customer in self.customers},
                fetch=fetch,
            )
            if snapshot is None:
                return self._empty("no_data")
            if fetch:
                self._last_fetch = now
            async_clear_issue(self.hass, self.entry, "power_planner_auth_required")
            async_clear_issue(self.hass, self.entry, "power_planner_unavailable")
            return PowerPlannerData(
                {
                    customer.stable_key: snapshot
                    if customer.house_contract_number == self.client.main_customer_number
                    else PowerPlannerSnapshot(status="unsupported_customer")
                    for customer in self.customers
                }
            )
        except KepcoOnAuthError:
            status = "auth_required"
            async_create_issue(self.hass, self.entry, "power_planner_auth_required")
        except KepcoOnRateLimitError:
            status = "rate_limited"
        except KepcoOnConnectionError:
            status = "connection_error"
        except KepcoOnProtocolError:
            status = "invalid_response"
        except KepcoOnUnsupportedAccount:
            status = "unsupported_profile"
        except KepcoOnNoCustomersError:
            status = "unsupported_customer"
        except OSError:
            status = "storage_error"
        if status != "auth_required":
            async_create_issue(self.hass, self.entry, "power_planner_unavailable")
        self._last_fetch = None
        return self._empty(status)
