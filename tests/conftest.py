"""Shared fixtures."""

from __future__ import annotations

import pytest

from .fake_panel import FakePanel


@pytest.fixture(scope="session", autouse=True)
def _pycares_shutdown_thread() -> None:
    """Start pycares' process-wide channel-shutdown thread before any test.

    Home Assistant resolves names with aiodns. The first failed connection (the
    config flow probing a web port that is closed) destroys a c-ares channel and
    that starts a daemon thread which then lives for the process. The test plugin
    treats a thread born during the test as a leak.
    """
    try:
        import pycares
    except ImportError:
        return
    manager = getattr(pycares, "_shutdown_manager", None)
    if manager is not None:
        manager.start()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components in every test."""
    yield


@pytest.fixture
async def panel(socket_enabled):
    fake = FakePanel()
    await fake.start()
    yield fake
    await fake.stop()
