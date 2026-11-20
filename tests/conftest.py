"""Shared fixtures for Home Assistant based tests."""

from __future__ import annotations

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Allow loading custom_components/ in all tests.

    The integration depends on the recorder, so an in-memory recorder is set
    up for every test (it must be requested before the hass fixture)."""
    return
