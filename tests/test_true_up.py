"""Truing cycles up to the bills the portal has posted, without being asked."""

from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.nisc_smarthub.store import store_key

from . import recordings
from .test_finalize import cost, one_hour_a_day, setup

pytestmark = pytest.mark.usefixtures("recorder_mock")

AFTER_OCTOBER_BILL = datetime(2026, 10, 25, 12, tzinfo=UTC)
SEPTEMBER_READ = datetime(2026, 9, 18, 4, tzinfo=UTC)
OCTOBER_READ = datetime(2026, 10, 18, 4, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def after_both_bills(
    hass: HomeAssistant, mock_client: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """Stand a week after October's bill, with an hour of usage every day."""
    await hass.config.async_set_time_zone(str(recordings.PORTAL_ZONE))
    freezer.move_to(AFTER_OCTOBER_BILL)
    mock_client.poll_hourly.return_value = recordings.poll_result(
        usage=one_hour_a_day(date(2026, 8, 28), date(2026, 10, 24))
    )


async def test_each_posted_bill_freezes_its_cycle_to_the_cent(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """Read dates bound the cycles, and each costs what its bill charged.

    The entry's cycle day is the 28th, so neither read date is a calendar
    boundary; the cents of Operation Round Up and the $30 establishment fee
    reach the statistics through the residual.
    """
    mock_client.billing_history.return_value = recordings.bills()

    await setup(hass, mock_config_entry)

    mock_client.billing_history.assert_awaited_with(
        recordings.ACCOUNT, recordings.CONNECT_DATE, AFTER_OCTOBER_BILL
    )
    assert await cost(hass, SEPTEMBER_READ) == pytest.approx(149.0, abs=1e-6)
    assert await cost(hass, OCTOBER_READ, since=SEPTEMBER_READ) == pytest.approx(
        145.0, abs=1e-6
    )
    records = mock_config_entry.runtime_data.cycle_records
    assert {day: record.actuals.values for day, record in records.items()} == {
        date(2026, 8, 28): {
            "service_charge": 23.1,
            "pca_factor": 0.0,
            "tax": pytest.approx(6.73),
        },
        date(2026, 9, 18): {
            "service_charge": 33.0,
            "pca_factor": 0.0,
            "tax": pytest.approx(8.16),
        },
    }


async def test_a_bill_posted_later_reprices_a_cycle_a_routine_run_skips(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """A routine run reaches a week back, but a new bill reaches its cycle."""
    await setup(hass, mock_config_entry)
    assert await cost(hass, SEPTEMBER_READ) != pytest.approx(149.0, abs=1e-6)

    mock_client.billing_history.return_value = recordings.bills()
    await mock_config_entry.runtime_data.async_refresh()
    await async_wait_recording_done(hass)

    plan = mock_config_entry.runtime_data.last_plan
    assert plan is not None
    assert plan.start == recordings.CONNECT_DATE

    assert await cost(hass, SEPTEMBER_READ) == pytest.approx(149.0, abs=1e-6)


async def test_a_bill_entered_by_hand_outranks_the_portal(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    hass_storage: dict[str, Any],
) -> None:
    """A record already covering the span stands; the other bill still lands."""
    hass_storage[store_key(mock_config_entry.entry_id)] = {
        "version": 1,
        "key": store_key(mock_config_entry.entry_id),
        "data": {
            "cycles": {
                "2026-08-28": {
                    "read_start": "2026-08-28",
                    "read_end": "2026-09-18",
                    "service_charge": 23.1,
                    "pca_factor": 0.0,
                    "tax": 6.73,
                    "bill_total": 150.0,
                    "residual": None,
                }
            }
        },
    }
    mock_client.billing_history.return_value = recordings.bills()

    await setup(hass, mock_config_entry)

    assert await cost(hass, SEPTEMBER_READ) == pytest.approx(150.0, abs=1e-6)
    assert await cost(hass, OCTOBER_READ, since=SEPTEMBER_READ) == pytest.approx(
        145.0, abs=1e-6
    )
