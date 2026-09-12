"""Shared fixtures for the test suite."""

from collections.abc import Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_LOCATION, CONF_PASSWORD
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension
from syrupy.assertion import SnapshotAssertion

from custom_components.nisc_smarthub.const import (
    CONF_ACCOUNT,
    CONF_CYCLE_DAY,
    CONF_RATE_VERSIONS,
    CONF_TARIFF,
    CONF_TOTP_SECRET,
    DOMAIN,
)
from custom_components.nisc_smarthub.tariff import NITEFLEX, rate_version_to_data

from . import recordings

# Inside the cycle that holds the recorded hours (2026-09-01 through 03, cycle
# day 28), so a test that never moves the clock sees them as the current cycle
# whatever the real date is.
FROZEN_NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


# Both syrupy and the Home Assistant test plugin define a `snapshot` fixture,
# and which one wins depends on plugin registration order, which follows the
# directory order of site-packages: sorted on APFS, hash order on ext4. On a
# Linux runner syrupy's own fixture can win and look for snapshots under
# `__snapshots__`. A conftest fixture outranks both, so the extension is
# chosen here, the way Home Assistant core's conftest does.
@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Return the snapshot assertion with Home Assistant's serializer and paths."""
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture(autouse=True)
def enable_custom_integration_loading(request: pytest.FixtureRequest) -> None:
    """Let Home Assistant load this repository's integration.

    Requested lazily so the pure client tests do not pay for a hass instance.
    """
    if "hass" in request.fixturenames:
        # The recorder has to be in place before custom integrations are
        # enabled, so order the two here rather than trusting argument order.
        if "recorder_mock" in request.fixturenames:
            request.getfixturevalue("recorder_mock")
        request.getfixturevalue("enable_custom_integrations")


@pytest.fixture(autouse=True)
def frozen_clock(request: pytest.FixtureRequest) -> None:
    """Freeze every entry-setup test at one instant inside the recorded cycle.

    A test that needs another instant asks for `freezer` and moves it.
    """
    if "hass" in request.fixturenames:
        freezer: FrozenDateTimeFactory = request.getfixturevalue("freezer")
        freezer.move_to(FROZEN_NOW)


@pytest.fixture
def mock_client() -> Generator[AsyncMock]:
    """Patch the portal client everywhere the integration constructs one."""
    client = AsyncMock()
    client.login.return_value = recordings.session()
    client.user_data.return_value = recordings.customers(recordings.LOCATION)
    client.billing.return_value = recordings.billing_summary()
    client.service_location.return_value = recordings.service_location()
    client.poll_hourly.return_value = recordings.poll_result()

    with (
        patch(
            "custom_components.nisc_smarthub.SmartHubClient", return_value=client
        ) as constructed,
        patch(
            "custom_components.nisc_smarthub.config_flow.SmartHubClient",
            new=constructed,
        ),
    ):
        yield client


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry for the recorded service location."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=recordings.DESCRIPTION,
        unique_id=f"{recordings.ACCOUNT}_{recordings.LOCATION}",
        data={
            CONF_HOST: recordings.HOST,
            CONF_EMAIL: recordings.EMAIL,
            CONF_PASSWORD: recordings.PASSWORD,
            CONF_TOTP_SECRET: recordings.TOTP_SECRET,
            CONF_ACCOUNT: recordings.ACCOUNT,
            CONF_LOCATION: recordings.LOCATION,
            CONF_CYCLE_DAY: 28,
            CONF_TARIFF: NITEFLEX.key,
            CONF_RATE_VERSIONS: [
                rate_version_to_data(NITEFLEX.newest_published_version())
            ],
        },
    )
