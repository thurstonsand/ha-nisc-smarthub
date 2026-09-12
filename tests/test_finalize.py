"""Freezing a cycle to its posted bill, and letting it go again."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.recorder import get_instance
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.nisc_smarthub.const import (
    CONF_CYCLE_DAY,
    CONF_RATE_VERSIONS,
    DOMAIN,
    SERVICE_FINALIZE_CYCLE,
    SERVICE_UNFINALIZE_CYCLE,
)
from custom_components.nisc_smarthub.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.nisc_smarthub.store import CycleStore, store_key
from custom_components.nisc_smarthub.tariff import NITEFLEX, rate_version_to_data

from . import recordings
from .test_init import IN_RECORDED_CYCLE, setup_entry

pytestmark = pytest.mark.usefixtures("recorder_mock", "mock_client")

CYCLE_START = date(2026, 8, 28)
NEXT_CYCLE = datetime(2026, 9, 28, 4, tzinfo=UTC)
LATER = datetime(2026, 10, 5, 12, tzinfo=UTC)

# The recorded hours end on the local night of 2026-09-03, so a bill read the
# next morning is the latest one the recorded data can carry.
READ_END = date(2026, 9, 4)
NOMINAL_HOURS = 7 * 24
PRICED_HOURS = 72

# The estimate the rate schedule produces for the recorded window, hand
# computed in `test_init.test_setup_writes_usage_per_period_and_cost`.
ESTIMATE = 20.15604
ENERGY = 15.51275

TAX = 1.5
BILL_TOTAL = 25.0


@pytest.fixture(autouse=True)
async def portal_timezone(hass: HomeAssistant) -> None:
    """Run these tests in the portal's zone, as the instance is configured."""
    await hass.config.async_set_time_zone(str(recordings.PORTAL_ZONE))


def bill(**overrides: Any) -> dict[str, Any]:
    """Return one posted bill for the cycle holding the recorded hours."""
    return {
        "cycle_start": CYCLE_START.isoformat(),
        "read_start": CYCLE_START.isoformat(),
        "read_end": READ_END.isoformat(),
        "service_charge": 33.0,
        "pca_factor": 0.0,
        "tax": TAX,
        "bill_total": BILL_TOTAL,
        **overrides,
    }


async def finalize(
    hass: HomeAssistant, entry: MockConfigEntry, **overrides: Any
) -> None:
    """Record a bill for the first cycle and let the run that prices it land."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_FINALIZE_CYCLE,
        {"config_entry_id": entry.entry_id, **bill(**overrides)},
        blocking=True,
    )
    await async_wait_recording_done(hass)


async def unfinalize(
    hass: HomeAssistant, entry: MockConfigEntry, cycle_start: date = CYCLE_START
) -> None:
    """Drop a cycle's bill and let the re-estimate land."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_UNFINALIZE_CYCLE,
        {"config_entry_id": entry.entry_id, "cycle_start": cycle_start.isoformat()},
        blocking=True,
    )
    await async_wait_recording_done(hass)


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set the entry up and let its first import land in the recorder."""
    await setup_entry(hass, entry)
    await async_wait_recording_done(hass)


def one_hour_a_day(first: date, last: date) -> dict[datetime, float]:
    """Return 1 kWh at 10:00 local on every day of a span, off peak."""
    usage: dict[datetime, float] = {}
    day = first
    while day <= last:
        local = datetime(
            day.year, day.month, day.day, 10, tzinfo=recordings.PORTAL_ZONE
        )
        usage[local.astimezone(UTC)] = 1.0
        day += timedelta(days=1)
    return usage


async def cost(
    hass: HomeAssistant,
    until: datetime | None = None,
    since: datetime = recordings.CONNECT_DATE,
) -> float:
    """Return the cost the recorder holds between two moments."""
    return await _sum_before(hass, until) - await _sum_before(hass, since)


async def _sum_before(hass: HomeAssistant, moment: datetime | None) -> float:
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        recordings.CONNECT_DATE,
        moment,
        {recordings.COST_STATISTIC_ID},
        "hour",
        None,
        {"sum"},
    )
    stored = rows.get(recordings.COST_STATISTIC_ID)
    if not stored:
        return 0.0
    total = stored[-1]["sum"]
    assert total is not None
    return total


def cycle_bounds(entry: MockConfigEntry) -> list[tuple[datetime, datetime]]:
    """Return the bounds of every cycle the last run covered."""
    plan = entry.runtime_data.last_plan
    assert plan is not None
    return [(one.start, one.end) for one in plan.cycles]


async def test_a_finalized_cycle_costs_exactly_its_bill(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The residual is what makes the cycle add up to what was charged."""
    await setup(hass, mock_config_entry)
    assert await cost(hass) == pytest.approx(ESTIMATE, abs=1e-5)

    await finalize(hass, mock_config_entry)

    assert await cost(hass) == pytest.approx(BILL_TOTAL, abs=1e-6)


async def test_unfinalizing_restores_the_estimate(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Without a bill the cycle is priced from the schedule again."""
    await setup(hass, mock_config_entry)
    await finalize(hass, mock_config_entry)

    await unfinalize(hass, mock_config_entry)

    assert await cost(hass) == pytest.approx(ESTIMATE, abs=1e-5)
    assert mock_config_entry.runtime_data.cycle_records == {}


async def test_the_residual_reaches_the_diagnostics(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A large residual is how a member learns the tariff is missing a line."""
    await setup(hass, mock_config_entry)
    await finalize(hass, mock_config_entry)

    records = (await async_get_config_entry_diagnostics(hass, mock_config_entry))[
        "cycle_records"
    ]

    # The bill's own service charge and tax are smeared over the cycle's
    # nominal hours, seven days of them, and 72 arrived.
    priced = ENERGY + (33.0 + TAX) * PRICED_HOURS / NOMINAL_HOURS
    assert records == {
        "2026-08-28": {
            "read_start": "2026-08-28T00:00:00-04:00",
            "read_end": "2026-09-04T00:00:00-04:00",
            "service_charge": 33.0,
            "pca_factor": 0.0,
            "tax": TAX,
            "bill_total": BILL_TOTAL,
            "residual": pytest.approx(BILL_TOTAL - priced, abs=1e-5),
        }
    }


@pytest.mark.parametrize(
    ("cycle_day", "cycle_start", "read_end", "expected"),
    [
        pytest.param(
            28,
            "2026-08-28",
            "2026-09-25",
            [
                (recordings.CONNECT_DATE, datetime(2026, 9, 25, 4, tzinfo=UTC)),
                (
                    datetime(2026, 9, 25, 4, tzinfo=UTC),
                    datetime(2026, 10, 28, 4, tzinfo=UTC),
                ),
            ],
            id="read_early",
        ),
        pytest.param(
            28,
            "2026-08-28",
            "2026-09-30",
            [
                (recordings.CONNECT_DATE, datetime(2026, 9, 30, 4, tzinfo=UTC)),
                (
                    datetime(2026, 9, 30, 4, tzinfo=UTC),
                    datetime(2026, 10, 28, 4, tzinfo=UTC),
                ),
            ],
            id="read_late",
        ),
        pytest.param(
            31,
            "2026-08-31",
            "2026-10-01",
            [
                (recordings.CONNECT_DATE, datetime(2026, 8, 31, 4, tzinfo=UTC)),
                (
                    datetime(2026, 8, 31, 4, tzinfo=UTC),
                    datetime(2026, 10, 1, 4, tzinfo=UTC),
                ),
                (
                    datetime(2026, 10, 1, 4, tzinfo=UTC),
                    datetime(2026, 10, 31, 4, tzinfo=UTC),
                ),
            ],
            id="read_past_the_clamped_day",
        ),
    ],
)
async def test_a_read_date_moves_the_boundary_nearest_to_it(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
    cycle_day: int,
    cycle_start: str,
    read_end: str,
    expected: list[tuple[datetime, datetime]],
) -> None:
    """The cycle after a bill runs from its read end to the next calendar day.

    A read date a few days off the calendar, one day past a clamped day
    included, moves the boundary rather than leaving a sliver beside it.
    """
    freezer.move_to(LATER)
    mock_client.poll_hourly.return_value = recordings.poll_result(
        usage=one_hour_a_day(CYCLE_START, date(2026, 10, 4))
    )
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, data=mock_config_entry.data | {CONF_CYCLE_DAY: cycle_day}
    )
    await setup(hass, mock_config_entry)

    await finalize(
        hass,
        mock_config_entry,
        cycle_start=cycle_start,
        read_start=cycle_start,
        read_end=read_end,
    )

    assert cycle_bounds(mock_config_entry) == expected
    finalized = expected[-2]
    assert await cost(hass, finalized[1], since=finalized[0]) == pytest.approx(
        BILL_TOTAL, abs=1e-6
    )


async def test_a_routine_run_leaves_a_finalized_cycle_alone(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The window opens at the cycle holding a week ago, and the bill holds."""
    freezer.move_to(LATER)
    mock_client.poll_hourly.return_value = recordings.poll_result(
        usage=one_hour_a_day(CYCLE_START, date(2026, 10, 4))
    )
    await setup(hass, mock_config_entry)
    await finalize(hass, mock_config_entry, read_end="2026-09-28")

    await mock_config_entry.runtime_data.async_refresh()
    await async_wait_recording_done(hass)

    plan = mock_config_entry.runtime_data.last_plan
    assert plan is not None
    assert plan.start == NEXT_CYCLE
    assert await cost(hass, NEXT_CYCLE) == pytest.approx(BILL_TOTAL, abs=1e-6)


async def test_a_rate_version_inside_a_finalized_cycle_reprices_only_the_next(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The bill is the finalized cycle's truth; the version governs from then on."""
    freezer.move_to(LATER)
    mock_client.poll_hourly.return_value = recordings.poll_result(
        usage=one_hour_a_day(CYCLE_START, date(2026, 10, 4))
    )
    await setup(hass, mock_config_entry)
    await finalize(hass, mock_config_entry, read_end="2026-09-28")
    # Seven off-peak hours sit in the next cycle, whose 30 days hold 720
    # hours of the service charge.
    before = 7 * 0.075 + 33.0 * 7 / 720
    assert await cost(hass) - BILL_TOTAL == pytest.approx(before * 1.0775, abs=1e-6)

    doubled = rate_version_to_data(NITEFLEX.newest_published_version())
    doubled["effective_from"] = "2026-09-02"
    doubled["off_peak_rate"] = 0.15
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data=mock_config_entry.data
        | {CONF_RATE_VERSIONS: [*mock_config_entry.data[CONF_RATE_VERSIONS], doubled]},
    )
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    after = 7 * 0.15 + 33.0 * 7 / 720
    assert await cost(hass, NEXT_CYCLE) == pytest.approx(BILL_TOTAL, abs=1e-6)
    assert await cost(hass) - BILL_TOTAL == pytest.approx(after * 1.0775, abs=1e-6)


async def test_removing_the_entry_drops_its_cycle_records(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Bills belong to the entry that recorded them."""
    await setup(hass, mock_config_entry)
    await finalize(hass, mock_config_entry)
    assert store_key(mock_config_entry.entry_id) in hass_storage

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert store_key(mock_config_entry.entry_id) not in hass_storage


async def test_a_finalized_cycle_is_reloaded_with_the_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A restart must not re-estimate a cycle whose bill has posted."""
    await setup(hass, mock_config_entry)
    await finalize(hass, mock_config_entry)

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    store = CycleStore(
        hass, entry_id=mock_config_entry.entry_id, zone=recordings.PORTAL_ZONE
    )
    await store.async_load()
    assert store.actuals_for(CYCLE_START) is not None
    assert await cost(hass) == pytest.approx(BILL_TOTAL, abs=1e-6)


async def test_a_bill_whose_run_failed_is_priced_after_a_reload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The record outlives the run that failed to price it, and so does the pass.

    A fresh coordinator would otherwise open at the first cycle without a
    bill, and the saved one would sit unpriced with the estimate still in the
    recorder.
    """
    await setup(hass, mock_config_entry)
    with (
        patch(
            "custom_components.nisc_smarthub.coordinator.StatisticsWriter.async_write",
            side_effect=RuntimeError("the recorder went away"),
        ),
        pytest.raises(HomeAssistantError, match="did not finish"),
    ):
        await finalize(hass, mock_config_entry)
    assert mock_config_entry.runtime_data.cycle_records[CYCLE_START].residual is None
    assert await cost(hass) == pytest.approx(ESTIMATE, abs=1e-5)

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    assert await cost(hass) == pytest.approx(BILL_TOTAL, abs=1e-6)
    record = mock_config_entry.runtime_data.cycle_records[CYCLE_START]
    assert record.residual is not None


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"cycle_start": "2026-09-15"}, id="no_cycle_starts_there"),
        pytest.param(
            {"read_start": "2026-09-04", "read_end": "2026-08-28"},
            id="read_dates_out_of_order",
        ),
        pytest.param({"read_end": "2026-08-28"}, id="a_cycle_of_no_hours"),
        pytest.param({"read_end": "2026-09-28"}, id="a_read_end_still_to_come"),
        pytest.param({"read_end": "2026-09-05"}, id="data_stops_before_the_read_end"),
        pytest.param({"bill_total": float("nan")}, id="a_total_that_is_not_a_number"),
        pytest.param({"tax": float("inf")}, id="a_tax_that_is_not_a_number"),
    ],
)
async def test_finalize_refuses_what_it_cannot_price(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    overrides: dict[str, Any],
) -> None:
    """A bill the integration cannot line up with a cycle is the caller's mistake."""
    freezer.move_to(IN_RECORDED_CYCLE)
    await setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError):
        await finalize(hass, mock_config_entry, **overrides)

    assert mock_config_entry.runtime_data.cycle_records == {}
    assert await cost(hass) == pytest.approx(ESTIMATE, abs=1e-5)


async def test_finalize_refuses_read_dates_that_overlap_another_bill(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Two bills cannot charge for the same hour."""
    await setup(hass, mock_config_entry)
    await finalize(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError, match="2026-08-28"):
        await finalize(
            hass,
            mock_config_entry,
            cycle_start="2026-09-04",
            read_start="2026-09-03",
            read_end="2026-09-04",
        )

    assert list(mock_config_entry.runtime_data.cycle_records) == [CYCLE_START]


async def test_unfinalize_refuses_a_cycle_that_was_never_finalized(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """There is no bill to drop, so there is nothing to reprice."""
    await setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError):
        await unfinalize(hass, mock_config_entry)
