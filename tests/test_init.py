"""Setting up, polling, and unloading one config entry."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.statistics import (
    list_statistic_ids,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.nisc_smarthub.const import (
    CONF_POLL_INTERVAL_MINUTES,
    CONF_RATE_VERSIONS,
    DOMAIN,
)
from custom_components.nisc_smarthub.smarthub import (
    AuthError,
    ClientError,
    MultipleMeters,
)

from . import recordings

pytestmark = pytest.mark.usefixtures("recorder_mock", "mock_client")

# Inside the cycle holding the recorded hours, for the tests that price it.
IN_RECORDED_CYCLE = datetime(2026, 9, 11, 12, tzinfo=UTC)


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add the entry to hass and set it up."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_writes_the_usage_statistic_and_unloads(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A loaded entry reconciles its history and lets go of it on unload."""
    await setup_entry(hass, mock_config_entry)
    await async_wait_recording_done(hass)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    ids = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, None, None
    )
    usage = next(
        one for one in ids if one["statistic_id"] == recordings.USAGE_STATISTIC_ID
    )
    assert usage["statistics_unit_of_measurement"] == "kWh"
    assert usage["unit_class"] == "energy"
    assert usage["has_sum"] is True
    assert usage["source"] == DOMAIN

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            AuthError("rejected"), ConfigEntryState.SETUP_ERROR, id="auth_failure"
        ),
        pytest.param(
            ClientError("no route"), ConfigEntryState.SETUP_RETRY, id="unreachable"
        ),
        pytest.param(
            MultipleMeters("got 2"), ConfigEntryState.SETUP_ERROR, id="unsupported"
        ),
    ],
)
async def test_a_failing_first_refresh_stops_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    error: Exception,
    expected: ConfigEntryState,
) -> None:
    """Reauth for rejected credentials, retry for the network, stop for a shape."""
    mock_client.login.side_effect = error

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is expected


async def test_the_poll_interval_option_is_read(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """A tuned interval takes effect without touching the entry's data."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_POLL_INTERVAL_MINUTES: 30}
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_client.poll_hourly.call_count == 1

    # The interval refresh runs as an entry background task, which a plain
    # block_till_done does not wait for.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=30))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_client.poll_hourly.call_count == 2


async def statistic_sums(hass: HomeAssistant, *ids: str) -> dict[str, float]:
    """Return the last cumulative sum of each statistic."""
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        recordings.CONNECT_DATE,
        None,
        set(ids),
        "hour",
        None,
        {"state", "sum"},
    )
    sums: dict[str, float] = {}
    for one in ids:
        total = rows[one][-1]["sum"]
        assert total is not None
        sums[one] = total
    return sums


async def statistic_hours(hass: HomeAssistant, statistic_id: str) -> list[float]:
    """Return every stored hourly value of one statistic."""
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        recordings.CONNECT_DATE,
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    values: list[float] = []
    for row in rows.get(statistic_id, []):
        state = row["state"]
        assert state is not None
        values.append(state)
    return values


async def test_setup_writes_usage_per_period_and_cost(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Five statistics: the total, one per period, and the dollars."""
    freezer.move_to(IN_RECORDED_CYCLE)
    await setup_entry(hass, mock_config_entry)
    await async_wait_recording_done(hass)

    ids = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, None, None
    )
    by_id = {one["statistic_id"]: one for one in ids}
    assert set(by_id) >= {
        recordings.USAGE_STATISTIC_ID,
        recordings.ON_PEAK_STATISTIC_ID,
        recordings.OFF_PEAK_STATISTIC_ID,
        recordings.SUPER_OFF_PEAK_STATISTIC_ID,
        recordings.COST_STATISTIC_ID,
    }
    cost = by_id[recordings.COST_STATISTIC_ID]
    assert cost["statistics_unit_of_measurement"] == "USD"
    assert cost["unit_class"] is None
    assert cost["has_sum"] is True

    sums = await statistic_sums(
        hass,
        recordings.USAGE_STATISTIC_ID,
        recordings.ON_PEAK_STATISTIC_ID,
        recordings.OFF_PEAK_STATISTIC_ID,
        recordings.SUPER_OFF_PEAK_STATISTIC_ID,
        recordings.COST_STATISTIC_ID,
    )
    assert sums[recordings.USAGE_STATISTIC_ID] == pytest.approx(157.55)
    assert sums[recordings.ON_PEAK_STATISTIC_ID] == pytest.approx(80.65)
    assert sums[recordings.OFF_PEAK_STATISTIC_ID] == pytest.approx(56.29)
    assert sums[recordings.SUPER_OFF_PEAK_STATISTIC_ID] == pytest.approx(20.61)

    # By hand, from the fixture's per-period kWh and the published rates. The
    # 20.61 super off-peak kWh sit inside the 400 kWh allowance, so they cost
    # nothing. The cycle runs 2026-08-28 to 09-28, 744 hours, and 72 of them
    # are priced, so the $33 service charge contributes 33 * 72 / 744.
    #   energy  = 80.65 * 0.14 + 56.29 * 0.075 = 11.291 + 4.22175 = 15.51275
    #   fixed   = 33 * 72 / 744                                   = 3.19355
    #   tax     = 0.0775 * (15.51275 + 3.19355)                   = 1.44974
    #   total                                                     = 20.15604
    assert sums[recordings.COST_STATISTIC_ID] == pytest.approx(20.15604, abs=1e-5)

    data = mock_config_entry.runtime_data.data
    assert sums[recordings.COST_STATISTIC_ID] == pytest.approx(data.cycle_cost.total)
    assert data.cycle_cost.energy > 0
    assert data.cycle_cost.fixed > 0
    assert data.cycle_cost.tax == pytest.approx(
        0.0775 * (data.cycle_cost.energy + data.cycle_cost.fixed)
    )
    assert data.allowance_remaining["super_off_peak"] == pytest.approx(
        400.0 - sums[recordings.SUPER_OFF_PEAK_STATISTIC_ID]
    )


async def test_an_unknown_period_label_raises_its_own_repair(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A label this tariff cannot price is a release's problem, and says so."""
    periods = recordings.hourly_periods()
    periods["Shoulder"] = periods.pop("Off Peak")
    mock_client.poll_hourly.return_value = recordings.poll_result(periods=periods)

    await setup_entry(hass, mock_config_entry)
    await async_wait_recording_done(hass)

    issue = issue_registry.async_get_issue(
        DOMAIN, f"unknown_period_label_{mock_config_entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["labels"] == "Shoulder"
    assert (
        issue_registry.async_get_issue(
            DOMAIN, f"unclassified_hours_{mock_config_entry.entry_id}"
        )
        is None
    )
    assert len(await statistic_hours(hass, recordings.USAGE_STATISTIC_ID)) == 72
    assert len(await statistic_hours(hass, recordings.COST_STATISTIC_ID)) == 42


async def test_a_newer_published_rate_version_asks_to_be_added(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Published rates are offered, never applied, and the offer clears."""
    stale = dict(mock_config_entry.data[CONF_RATE_VERSIONS][0])
    current = stale["effective_from"]
    stale["effective_from"] = "2025-01-01"
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data=mock_config_entry.data | {CONF_RATE_VERSIONS: [stale]},
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    issue_id = f"newer_rate_version_{mock_config_entry.entry_id}"
    issue = issue_registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["effective_from"] == current

    hass.config_entries.async_update_entry(
        mock_config_entry,
        data=mock_config_entry.data
        | {CONF_RATE_VERSIONS: [stale | {"effective_from": current}]},
    )
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


async def test_the_shipped_rate_version_raises_no_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """An entry configured with the newest published rates is up to date."""
    await setup_entry(hass, mock_config_entry)

    assert (
        issue_registry.async_get_issue(
            DOMAIN, f"newer_rate_version_{mock_config_entry.entry_id}"
        )
        is None
    )
