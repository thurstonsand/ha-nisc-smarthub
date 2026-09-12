"""What the entity surface shows, and that it is never the point of the entry."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.nisc_smarthub.const import DEFAULT_POLL_INTERVAL_MINUTES, DOMAIN
from custom_components.nisc_smarthub.entity import MANUFACTURER
from custom_components.nisc_smarthub.tariff import NITEFLEX

from . import recordings
from .test_init import setup_entry, statistic_sums

pytestmark = pytest.mark.usefixtures("recorder_mock", "mock_client")

# Inside the recorded window, so the current cycle holds the recorded hours.
NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def portal_timezone(hass: HomeAssistant) -> None:
    """Run these tests in the portal's zone, as the instance is configured."""
    await hass.config.async_set_time_zone(str(recordings.PORTAL_ZONE))


async def test_every_sensor_matches_its_snapshot(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The states and registry entries one reconciled window produces."""
    freezer.move_to(NOW)

    await setup_entry(hass, mock_config_entry)

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


async def test_the_cost_sensor_agrees_with_the_cost_statistic(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The cycle's sensors are the statistics the same run wrote."""
    freezer.move_to(NOW)

    await setup_entry(hass, mock_config_entry)
    sums = await statistic_sums(
        hass,
        recordings.COST_STATISTIC_ID,
        recordings.USAGE_STATISTIC_ID,
        recordings.SUPER_OFF_PEAK_STATISTIC_ID,
    )

    cost = hass.states.get("sensor.example_premise_cycle_to_date_cost")
    assert cost is not None
    assert float(cost.state) == pytest.approx(sums[recordings.COST_STATISTIC_ID])
    assert cost.attributes["unit_of_measurement"] == "USD"
    assert cost.attributes["device_class"] == "monetary"
    # The statistics are the long-term record; the sensors claim none of
    # their own.
    assert "state_class" not in cost.attributes
    assert "last_reset" not in cost.attributes
    assert cost.attributes["priced_hours"] == 72
    assert cost.attributes["unpriced_hours"] == 0
    assert sum(
        cost.attributes[component]
        for component in ("energy", "fixed", "rider", "tax", "adjustment", "residual")
    ) == pytest.approx(float(cost.state), abs=1e-4)

    usage = hass.states.get("sensor.example_premise_cycle_to_date_usage")
    assert usage is not None
    assert float(usage.state) == pytest.approx(sums[recordings.USAGE_STATISTIC_ID])
    assert usage.attributes["unit_of_measurement"] == "kWh"
    assert "state_class" not in usage.attributes

    allowance = hass.states.get(
        "sensor.example_premise_super_off_peak_allowance_remaining"
    )
    assert allowance is not None
    assert float(allowance.state) == pytest.approx(
        400.0 - sums[recordings.SUPER_OFF_PEAK_STATISTIC_ID]
    )
    assert "state_class" not in allowance.attributes

    newest = hass.states.get("sensor.example_premise_newest_data_hour")
    assert newest is not None
    assert newest.state == max(recordings.hourly_usage()).isoformat()


async def test_the_device_names_the_location_the_tariff_and_the_meter(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """One device per entry, identified by account and service location."""
    freezer.move_to(NOW)

    await setup_entry(hass, mock_config_entry)

    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{recordings.ACCOUNT}_{recordings.LOCATION}"),
        mock_config_entry.entry_id,
    )
    assert device is not None
    assert device.name == recordings.DESCRIPTION
    assert device.manufacturer == MANUFACTURER
    assert device.model == NITEFLEX.display_name
    assert device.serial_number == recordings.METER
    assert device.configuration_url == f"https://{recordings.HOST}/"


async def test_polling_continues_with_every_sensor_disabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The statistics are the product, so the entry listens for itself."""
    freezer.move_to(NOW)
    await setup_entry(hass, mock_config_entry)
    entries = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )
    assert entries

    for entry in entries:
        entity_registry.async_update_entity(
            entry.entity_id, disabled_by=er.RegistryEntryDisabler.USER
        )
    await hass.async_block_till_done()
    assert not hass.states.async_entity_ids("sensor")
    assert mock_client.poll_hourly.call_count == 1

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(minutes=DEFAULT_POLL_INTERVAL_MINUTES)
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_client.poll_hourly.call_count == 2
