"""Shared fixtures."""

from __future__ import annotations

import functools
from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest

from custom_components.clearlight_sauna import protocol as p

from .fake_gagent import FAKE_DID, FAKE_MAC, FakeGAgent

try:  # only present when the HA test harness is installed
    import pytest_homeassistant_custom_component  # noqa: F401

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        yield

except ImportError:  # pragma: no cover
    pass


@pytest.fixture
async def fake_device() -> AsyncGenerator[FakeGAgent]:
    dev = FakeGAgent()
    await dev.start()
    yield dev
    await dev.stop()


@pytest.fixture
def patch_client(fake_device: FakeGAgent):
    """Point every ClearlightClient at the fake device's port; stub discovery."""
    client_cls = functools.partial(p.ClearlightClient, port=fake_device.port)
    discovered = p.DiscoveredDevice(
        host="127.0.0.1",
        did=FAKE_DID,
        mac=FAKE_MAC,
        product_key=p.CLEARLIGHT_PRODUCT_KEY,
        api_server="usapi.gizwits.com:80",
        gagent_version="4.1.2",
    )

    async def fake_discover(timeout=3.0, targets=("255.255.255.255",), product_key=None):
        return [discovered]

    with (
        patch("custom_components.clearlight_sauna.ClearlightClient", client_cls),
        patch("custom_components.clearlight_sauna.config_flow.ClearlightClient", client_cls),
        patch("custom_components.clearlight_sauna.async_discover", fake_discover),
        patch("custom_components.clearlight_sauna.config_flow.async_discover", fake_discover),
    ):
        yield
