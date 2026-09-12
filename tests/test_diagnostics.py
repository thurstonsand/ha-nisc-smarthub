"""What the entry dump carries, and what it must never carry."""

from datetime import UTC, datetime
import json
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from syrupy.assertion import SnapshotAssertion

from custom_components.nisc_smarthub.const import DOMAIN, ISSUE_UNKNOWN_PERIOD_LABEL
from custom_components.nisc_smarthub.diagnostics import (
    async_get_config_entry_diagnostics,
)

from . import recordings
from .test_init import setup_entry

pytestmark = pytest.mark.usefixtures("recorder_mock", "mock_client")

NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)

# Every identifier the recordings put in play, whether or not the dump has a
# place for it today.
IDENTIFIERS = (
    recordings.ACCOUNT,
    recordings.LOCATION,
    recordings.METER,
    recordings.PASSWORD,
    recordings.TOTP_SECRET,
    recordings.EMAIL,
    "Pat Member",
    "100 Example Rd",
)


@pytest.fixture(autouse=True)
async def portal_timezone(hass: HomeAssistant) -> None:
    """Run these tests in the portal's zone, as the instance is configured."""
    await hass.config.async_set_time_zone(str(recordings.PORTAL_ZONE))


async def dump(
    hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> dict[str, Any]:
    """Set the entry up and return its diagnostics."""
    freezer.move_to(NOW)
    await setup_entry(hass, entry)
    return await async_get_config_entry_diagnostics(hass, entry)


async def test_the_dump_matches_its_snapshot(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    snapshot: SnapshotAssertion,
    freezer: FrozenDateTimeFactory,
) -> None:
    """One reconciled window, described for someone else to read."""
    assert await dump(hass, mock_config_entry, freezer) == snapshot


async def test_no_identifier_survives_the_dump(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Diagnostics get pasted into public issues."""
    serialized = json.dumps(await dump(hass, mock_config_entry, freezer))

    for identifier in IDENTIFIERS:
        assert identifier not in serialized

    assert "**REDACTED_account**" in serialized
    assert "**REDACTED_location**" in serialized
    assert "**REDACTED_meter**" in serialized


async def test_a_repair_issue_reaches_the_dump_without_its_identifiers(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An issue's placeholders can spell an identifier, so they are walked too.

    A period label the portal reports carries the meter number in front of
    it, which is the shape an unknown label would arrive in.
    """
    freezer.move_to(NOW)
    await setup_entry(hass, mock_config_entry)
    issue_id = f"{ISSUE_UNKNOWN_PERIOD_LABEL}_{mock_config_entry.entry_id}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_UNKNOWN_PERIOD_LABEL,
        translation_placeholders={
            "labels": f"{recordings.METER} - Shoulder",
            "tariff": "NiteFlex",
        },
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    issues = diagnostics["issues"]
    assert [one["issue_id"] for one in issues] == [issue_id]
    assert issues[0]["translation_placeholders"]["labels"] == (
        "**REDACTED_meter** - Shoulder"
    )
    assert recordings.METER not in json.dumps(diagnostics)
