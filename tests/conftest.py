"""Test package configuration for the KEPCO ON integration."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.core_config import DATA_CUSTOMIZE
from homeassistant.helpers import device_registry, entity_registry, frame
from homeassistant.helpers.entity_values import EntityValues


@pytest_asyncio.fixture
async def hass(tmp_path: Path) -> AsyncIterator[HomeAssistant]:
    """Provide the real HA state machine for sensor and template regressions."""
    instance = HomeAssistant(str(tmp_path))
    loader.async_setup(instance)
    frame.async_setup(instance)
    instance.config_entries = ConfigEntries(instance, {})
    instance.data[DATA_CUSTOMIZE] = EntityValues({})
    device_registry.async_setup(instance)
    await device_registry.async_load(instance, load_empty=True)
    await entity_registry.async_load(instance, load_empty=True)
    try:
        yield instance
    finally:
        await instance.async_stop(force=True)


@pytest.fixture
def enable_custom_integrations(hass: HomeAssistant) -> None:
    """Let the loader resolve this repository's custom integration."""
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
