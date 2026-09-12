"""The writer against the real recorder."""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
import itertools
import logging
from unittest.mock import MagicMock, patch

from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.nisc_smarthub.tariff import NITEFLEX
from custom_components.nisc_smarthub.writer import (
    COST_KIND,
    USAGE_KIND,
    StatisticsWriter,
    owned_series,
    period_kind,
)

from . import recordings

FLOOR = datetime(2026, 9, 1, 4, tzinfo=UTC)
HOUR = timedelta(hours=1)
ENTRY_ID = "entry-under-test"

pytestmark = pytest.mark.usefixtures("recorder_mock")


@pytest.fixture
def writer(hass: HomeAssistant) -> StatisticsWriter:
    """Return a writer owning a usage and a cost series, and nothing per period."""
    return StatisticsWriter(
        hass,
        entry_id=ENTRY_ID,
        series=owned_series(
            account=recordings.ACCOUNT,
            location=recordings.LOCATION,
            service_description=recordings.DESCRIPTION,
            periods=(),
        ),
    )


@pytest.fixture
def imports() -> Generator[MagicMock]:
    """Count the writer's imports without replacing them."""
    with patch(
        "custom_components.nisc_smarthub.writer.async_add_external_statistics",
        wraps=async_add_external_statistics,
    ) as spy:
        yield spy


def hours(*values: float, first: datetime = FLOOR) -> dict[datetime, float]:
    """Return consecutive hourly values starting at an hour."""
    return {first + index * HOUR: value for index, value in enumerate(values)}


async def reconcile(
    hass: HomeAssistant,
    writer: StatisticsWriter,
    usage: dict[datetime, float],
    cost: dict[datetime, float] | None = None,
    *,
    window_start: datetime = FLOOR,
) -> None:
    """Run one reconcile pass over the given hours.

    The cost series gets the usage doubled unless told otherwise, so both
    owned series carry rows.
    """
    values = {
        recordings.USAGE_STATISTIC_ID: usage,
        recordings.COST_STATISTIC_ID: (
            {hour: 2 * value for hour, value in usage.items()} if cost is None else cost
        ),
    }
    seeds = await writer.async_read_seeds(window_start=window_start, floor=FLOOR)
    assert seeds is not None
    await writer.async_write(values, seeds, window_start=window_start)
    await async_wait_recording_done(hass)


async def stored(
    hass: HomeAssistant, statistic_id: str = recordings.USAGE_STATISTIC_ID
) -> list[tuple[datetime, float, float]]:
    """Return every stored row of one statistic as (hour, state, sum)."""
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        FLOOR,
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    read: list[tuple[datetime, float, float]] = []
    for row in rows.get(statistic_id, []):
        state, total = row["state"], row["sum"]
        assert state is not None
        assert total is not None
        read.append((datetime.fromtimestamp(row["start"], UTC), state, total))
    return read


def states(rows: list[tuple[datetime, float, float]]) -> list[float]:
    """Return the hourly values of stored rows."""
    return [row[1] for row in rows]


def sums(rows: list[tuple[datetime, float, float]]) -> list[float]:
    """Return the cumulative totals of stored rows."""
    return [row[2] for row in rows]


def test_a_tariff_with_periods_owns_one_series_per_period() -> None:
    """The series set is fixed by the tariff, before any poll."""
    series = owned_series(
        account=recordings.ACCOUNT,
        location=recordings.LOCATION,
        service_description=recordings.DESCRIPTION,
        periods=NITEFLEX.periods,
    )

    assert [one.kind for one in series] == [
        USAGE_KIND,
        period_kind("on_peak"),
        period_kind("off_peak"),
        period_kind("super_off_peak"),
        COST_KIND,
    ]
    assert [one.statistic_id for one in series] == [
        recordings.USAGE_STATISTIC_ID,
        recordings.ON_PEAK_STATISTIC_ID,
        recordings.OFF_PEAK_STATISTIC_ID,
        recordings.SUPER_OFF_PEAK_STATISTIC_ID,
        recordings.COST_STATISTIC_ID,
    ]
    assert series[0].name == "Example Premise usage"
    assert series[1].name == "Example Premise on peak usage"
    assert series[-1].unit_class is None


def test_two_series_of_one_kind_are_refused(hass: HomeAssistant) -> None:
    """Logs name a series by kind, so kinds have to be unambiguous."""
    series = owned_series(
        account=recordings.ACCOUNT,
        location=recordings.LOCATION,
        service_description=recordings.DESCRIPTION,
        periods=(),
    )

    with pytest.raises(ValueError, match="unique"):
        StatisticsWriter(hass, entry_id=ENTRY_ID, series=[series[0], series[0]])


async def test_the_first_import_starts_its_sum_at_the_floor(
    hass: HomeAssistant, writer: StatisticsWriter
) -> None:
    """Every recorded hour arrives, with sums cumulative from zero."""
    await reconcile(hass, writer, recordings.hourly_usage())

    rows = await stored(hass)
    assert len(rows) == 72
    assert rows[0][0] == FLOOR
    assert rows[-1][2] == pytest.approx(157.55)
    assert all(later[2] >= earlier[2] for earlier, later in itertools.pairwise(rows))


async def test_reimporting_the_same_hours_writes_nothing(
    hass: HomeAssistant, writer: StatisticsWriter, imports: MagicMock
) -> None:
    """A run that agrees with the recorder leaves it alone."""
    usage = hours(1.0, 2.0, 3.0)
    await reconcile(hass, writer, usage)
    assert imports.call_count == 2

    await reconcile(hass, writer, usage)

    assert imports.call_count == 2
    assert sums(await stored(hass)) == [1.0, 3.0, 6.0]


async def test_a_revised_hour_rewrites_every_later_sum(
    hass: HomeAssistant,
    writer: StatisticsWriter,
    imports: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A changed hour carries its correction through the tail."""
    await reconcile(hass, writer, hours(1.0, 2.0, 3.0))

    with caplog.at_level(logging.DEBUG, "custom_components.nisc_smarthub.writer"):
        await reconcile(hass, writer, hours(1.0, 5.0, 3.0))

    rows = await stored(hass)
    assert states(rows) == [1.0, 5.0, 3.0]
    assert sums(rows) == [1.0, 6.0, 9.0]
    assert imports.call_count == 4
    summary = next(
        record
        for record in caplog.records
        if record.levelno == logging.INFO and "revised" in record.getMessage()
    )
    assert f"the usage series of entry {ENTRY_ID} revised 1 hours" in (
        summary.getMessage()
    )
    detail = next(
        record for record in caplog.records if "from 2.0 to 5.0" in record.getMessage()
    )
    assert detail.levelno == logging.DEBUG


async def test_an_hour_the_portal_never_reported_gets_no_row(
    hass: HomeAssistant, writer: StatisticsWriter
) -> None:
    """A missing hour stays missing rather than being written as a zero."""
    usage = hours(1.0, 2.0)
    usage[FLOOR + 3 * HOUR] = 4.0

    await reconcile(hass, writer, usage)

    rows = await stored(hass)
    assert [row[0] for row in rows] == [FLOOR, FLOOR + HOUR, FLOOR + 3 * HOUR]
    assert sums(rows) == [1.0, 3.0, 7.0]


async def test_a_stored_hour_the_poll_dropped_becomes_a_zero_row(
    hass: HomeAssistant, writer: StatisticsWriter, caplog: pytest.LogCaptureFixture
) -> None:
    """An interior hour that vanished is zeroed, and the sum carries through it."""
    await reconcile(hass, writer, hours(1.0, 2.0, 3.0))
    usage = hours(1.0, 2.0, 3.0)
    del usage[FLOOR + HOUR]

    with caplog.at_level(logging.WARNING, "custom_components.nisc_smarthub.writer"):
        await reconcile(hass, writer, usage)

    rows = await stored(hass)
    assert states(rows) == [1.0, 0.0, 3.0]
    assert sums(rows) == [1.0, 1.0, 4.0]
    assert (
        f"the usage series of entry {ENTRY_ID} holds 1 hours from "
        f"{(FLOOR + HOUR).isoformat()} to {(FLOOR + HOUR).isoformat()}"
    ) in caplog.text
    assert f"the cost series of entry {ENTRY_ID} holds 1 hours" in caplog.text


async def test_a_dropped_tail_becomes_zero_rows(
    hass: HomeAssistant, writer: StatisticsWriter
) -> None:
    """Rows past the newest hour the poll carries are zeroed, not left behind."""
    await reconcile(hass, writer, hours(1.0, 2.0, 3.0, 4.0))

    await reconcile(hass, writer, hours(1.0, 2.0))

    rows = await stored(hass)
    assert states(rows) == [1.0, 2.0, 0.0, 0.0]
    assert sums(rows) == [1.0, 3.0, 3.0, 3.0]


async def test_a_series_the_poll_lacks_entirely_is_left_alone(
    hass: HomeAssistant, writer: StatisticsWriter, imports: MagicMock
) -> None:
    """Only the usage series can withdraw an hour; a silent cost series cannot."""
    await reconcile(hass, writer, hours(1.0, 2.0))
    assert imports.call_count == 2

    await reconcile(hass, writer, hours(1.0, 2.0), cost={})

    assert imports.call_count == 2
    assert sums(await stored(hass)) == [1.0, 3.0]
    cost = await stored(hass, recordings.COST_STATISTIC_ID)
    assert states(cost) == [2.0, 4.0]
    assert sums(cost) == [2.0, 6.0]


async def test_a_stored_hour_one_series_has_no_value_for_keeps_its_row(
    hass: HomeAssistant, writer: StatisticsWriter, imports: MagicMock
) -> None:
    """The row stays, and the rows after it carry its value in their sums."""
    await reconcile(hass, writer, hours(1.0, 2.0, 3.0))
    assert imports.call_count == 2
    partial = {FLOOR: 2.0, FLOOR + 2 * HOUR: 6.0}

    await reconcile(hass, writer, hours(1.0, 2.0, 3.0), cost=partial)
    assert imports.call_count == 2

    partial[FLOOR + 2 * HOUR] = 7.0
    await reconcile(hass, writer, hours(1.0, 2.0, 3.0), cost=partial)

    cost = await stored(hass, recordings.COST_STATISTIC_ID)
    assert states(cost) == [2.0, 4.0, 7.0]
    assert sums(cost) == [2.0, 6.0, 13.0]


async def test_an_empty_poll_zeroes_every_stored_hour_in_the_window(
    hass: HomeAssistant, writer: StatisticsWriter
) -> None:
    """Nothing reported means every stored hour is one the portal took back."""
    await reconcile(hass, writer, hours(1.0, 2.0, 3.0))

    await reconcile(hass, writer, {}, cost={})

    rows = await stored(hass)
    assert states(rows) == [0.0, 0.0, 0.0]
    assert sums(rows) == [0.0, 0.0, 0.0]


async def test_an_hour_the_portal_reports_again_comes_back(
    hass: HomeAssistant, writer: StatisticsWriter
) -> None:
    """A zeroed hour is an ordinary revision once the portal has it again."""
    await reconcile(hass, writer, hours(1.0, 2.0, 3.0))
    usage = hours(1.0, 2.0, 3.0)
    del usage[FLOOR + HOUR]
    await reconcile(hass, writer, usage)
    assert sums(await stored(hass)) == [1.0, 1.0, 4.0]

    await reconcile(hass, writer, hours(1.0, 2.0, 3.0))

    rows = await stored(hass)
    assert states(rows) == [1.0, 2.0, 3.0]
    assert sums(rows) == [1.0, 3.0, 6.0]


async def test_a_zero_row_already_written_is_not_warned_about_again(
    hass: HomeAssistant, writer: StatisticsWriter, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning is for the run that zeroes an hour, not every run after."""
    await reconcile(hass, writer, hours(1.0, 2.0, 3.0))
    usage = hours(1.0, 2.0, 3.0)
    del usage[FLOOR + HOUR]
    await reconcile(hass, writer, usage)
    caplog.clear()

    with caplog.at_level(logging.WARNING, "custom_components.nisc_smarthub.writer"):
        await reconcile(hass, writer, usage)

    assert "no longer reports" not in caplog.text


async def test_a_seed_reads_the_last_sum_before_the_window(
    hass: HomeAssistant, writer: StatisticsWriter
) -> None:
    """A later window continues the cumulative total it inherits."""
    await reconcile(hass, writer, hours(1.0, 2.0, 3.0))

    window_start = FLOOR + 2 * HOUR
    await reconcile(
        hass, writer, hours(10.0, first=window_start), window_start=window_start
    )

    assert sums(await stored(hass)) == [1.0, 3.0, 13.0]


async def test_an_empty_usage_seed_read_past_the_floor_is_a_gap(
    hass: HomeAssistant, writer: StatisticsWriter
) -> None:
    """No usage stored before the window means the run cannot seed honestly."""
    seeds = await writer.async_read_seeds(window_start=FLOOR + 2 * HOUR, floor=FLOOR)

    assert seeds is None


async def test_a_series_with_nothing_before_the_window_seeds_at_zero(
    hass: HomeAssistant, writer: StatisticsWriter
) -> None:
    """A cost series that has had nothing to say yet is not a gap."""
    await reconcile(hass, writer, hours(1.0, 2.0), cost={})

    seeds = await writer.async_read_seeds(window_start=FLOOR + 2 * HOUR, floor=FLOOR)

    assert seeds == {
        recordings.USAGE_STATISTIC_ID: 3.0,
        recordings.COST_STATISTIC_ID: 0.0,
    }


async def test_a_window_at_the_floor_seeds_at_zero(
    hass: HomeAssistant, writer: StatisticsWriter
) -> None:
    """The history floor is where cumulative sums start."""
    seeds = await writer.async_read_seeds(window_start=FLOOR, floor=FLOOR)

    assert seeds == {
        recordings.USAGE_STATISTIC_ID: 0.0,
        recordings.COST_STATISTIC_ID: 0.0,
    }
