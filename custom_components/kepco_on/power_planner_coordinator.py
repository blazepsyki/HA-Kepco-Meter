"""Refresh the optional planner independently from monthly billing."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_USERNAME, DOMAIN, POWER_PLANNER_DEFAULT_SOURCES, POWER_PLANNER_SOURCE_KEYS
from .exceptions import (
    KepcoOnAuthError,
    KepcoOnConnectionError,
    KepcoOnNoCustomersError,
    KepcoOnProtocolError,
    KepcoOnRateLimitError,
    KepcoOnUnsupportedAccount,
)
from .models import KepcoCustomer, PowerPlannerData, PowerPlannerSnapshot
from .power_planner import KOREA, PowerPlannerClient
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
        self._last_sources: frozenset[str] = frozenset()

    def _enabled_sources(self) -> frozenset[str]:
        """Respect registry choices, including defaults before first platform setup."""
        registry = er.async_get(self.hass)
        entries = {
            entry.unique_id: entry
            for entry in er.async_entries_for_config_entry(registry, self.entry.entry_id)
        }
        enabled: set[str] = set()
        for source, keys in POWER_PLANNER_SOURCE_KEYS.items():
            for customer in self.customers:
                if source == "neighbors" and not customer.is_house:
                    continue
                for key in keys:
                    entry = entries.get(f"{customer.stable_key}_{key}")
                    if (entry is not None and entry.disabled_by is None) or (
                        entry is None and source in POWER_PLANNER_DEFAULT_SOURCES
                    ):
                        enabled.add(source)
        return frozenset(enabled)

    def _empty(self, status: str) -> PowerPlannerData:
        return PowerPlannerData(
            {
                customer.stable_key: PowerPlannerSnapshot(status=status)
                for customer in self.customers
            }
        )

    async def _async_update_data(self) -> PowerPlannerData:
        now = datetime.now(UTC)
        sources = self._enabled_sources()
        data = getattr(self, "data", None)
        new_day = isinstance(data, PowerPlannerData) and any(
            snapshot.calendar_date is not None
            and snapshot.calendar_date != now.astimezone(KOREA).date()
            for snapshot in data.snapshots.values()
        )
        fetch = (
            self._last_fetch is None
            or now - self._last_fetch >= timedelta(hours=1)
            or new_day
            or bool(sources - self._last_sources)
        )
        try:
            if not self.customers:
                raise KepcoOnNoCustomersError("Power Planner bound customer is not selected")
            snapshot = await self.client.async_poll(
                str(self.entry.data[CONF_USERNAME]),
                self._password,
                {customer.house_contract_number for customer in self.customers},
                fetch=fetch,
                sources=sources,
            )
            if snapshot is None:
                return self._empty("no_data")
            if fetch:
                self._last_fetch = now
            self._last_sources = sources
            async_clear_issue(self.hass, self.entry, "power_planner_auth_required")
            if any(
                status
                in {
                    "connection_error",
                    "rate_limited",
                    "invalid_response",
                    "period_mismatch",
                    "comparison_scope_unverified",
                }
                for status in snapshot.field_statuses.values()
            ):
                async_create_issue(self.hass, self.entry, "power_planner_unavailable")
            else:
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
