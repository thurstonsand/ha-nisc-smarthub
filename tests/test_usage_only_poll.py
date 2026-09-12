"""A poll with no classification, run through the real client parser."""

from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.nisc_smarthub.const import DOMAIN

from . import recordings
from .smarthub.test_client import login_response, usage_only
from .test_init import setup_entry, statistic_hours

pytestmark = pytest.mark.usefixtures("recorder_mock")

BASE = f"https://{recordings.HOST}/services"


def serve_portal(aioclient_mock: AiohttpClientMocker, poll: Any) -> None:
    """Answer the calls one reconcile run makes with recorded responses."""
    aioclient_mock.post(f"{BASE}/oauth/auth/v2", json=login_response())
    aioclient_mock.get(f"{BASE}/secured/billing", json=recordings.payload("billing"))
    aioclient_mock.post(f"{BASE}/secured/utility-usage/poll", json=poll)


async def test_a_usage_only_poll_writes_usage_and_raises_the_repair(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Every hour has kWh, none has a cost, and the hole is every hour."""
    await hass.config.async_set_time_zone(str(recordings.PORTAL_ZONE))
    serve_portal(aioclient_mock, usage_only(recordings.payload("poll_hourly")))

    await setup_entry(hass, mock_config_entry)
    await async_wait_recording_done(hass)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert len(await statistic_hours(hass, recordings.USAGE_STATISTIC_ID)) == 72
    assert await statistic_hours(hass, recordings.COST_STATISTIC_ID) == []
    assert await statistic_hours(hass, recordings.ON_PEAK_STATISTIC_ID) == []
    issue = issue_registry.async_get_issue(
        DOMAIN, f"unclassified_hours_{mock_config_entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["count"] == "72"
