"""Which hours a run reconciles, and what makes it reach further back."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import threading
from typing import Any, override
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.core import Recorder
from homeassistant.components.recorder.db_schema import Statistics, StatisticsMeta
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.recorder import get_instance
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.nisc_smarthub.const import (
    DOMAIN,
    SERVICE_FINALIZE_CYCLE,
    SERVICE_RECONCILE,
)
from custom_components.nisc_smarthub.coordinator import FullRequest, plan_window
from custom_components.nisc_smarthub.smarthub import ClientError

from . import recordings
from .test_finalize import one_hour_a_day
from .test_init import IN_RECORDED_CYCLE, setup_entry, statistic_hours, statistic_sums

pytestmark = pytest.mark.usefixtures("recorder_mock", "mock_client")

# A week after the recorded window, so the cycle holding seven days ago is the
# one that starts on the cycle day rather than the floor's partial first cycle.
NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)
CYCLE_START = datetime(2026, 9, 28, 4, tzinfo=UTC)
HOUR = timedelta(hours=1)


@pytest.fixture(autouse=True)
async def portal_timezone(hass: HomeAssistant) -> None:
    """Run these tests in the portal's zone, as the instance is configured."""
    await hass.config.async_set_time_zone(str(recordings.PORTAL_ZONE))


def requested_start(client: AsyncMock) -> datetime:
    """Return the first hour the last poll asked the portal for."""
    return client.poll_hourly.call_args.args[2]


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set the entry up and let its first import land in the recorder."""
    await setup_entry(hass, entry)
    await async_wait_recording_done(hass)


async def refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Run one more reconcile pass."""
    await entry.runtime_data.async_refresh()
    await async_wait_recording_done(hass)


async def test_the_first_run_is_full_and_the_next_is_routine(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Setup covers the history; ordinary runs cover a week and its cycle."""
    freezer.move_to(NOW)
    with caplog.at_level(logging.DEBUG, "custom_components.nisc_smarthub.coordinator"):
        await setup(hass, mock_config_entry)

        assert requested_start(mock_client) == recordings.CONNECT_DATE
        assert "reconciling a full window" in caplog.text

        await refresh(hass, mock_config_entry)

    assert requested_start(mock_client) == CYCLE_START
    assert "reconciling a routine window" in caplog.text


async def test_a_requested_full_pass_reaches_the_floor_once(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The request is honoured by the next run and not by the one after it."""
    freezer.move_to(NOW)
    await setup(hass, mock_config_entry)
    await refresh(hass, mock_config_entry)
    assert requested_start(mock_client) == CYCLE_START

    mock_config_entry.runtime_data.request_full(None)
    await refresh(hass, mock_config_entry)
    assert requested_start(mock_client) == recordings.CONNECT_DATE

    await refresh(hass, mock_config_entry)
    assert requested_start(mock_client) == CYCLE_START


async def test_a_failed_run_keeps_the_full_pass_pending(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A request outlives the run that could not carry it out."""
    freezer.move_to(NOW)
    await setup(hass, mock_config_entry)

    mock_config_entry.runtime_data.request_full(None)
    mock_client.poll_hourly.side_effect = ClientError("no route")
    await refresh(hass, mock_config_entry)
    assert not mock_config_entry.runtime_data.last_update_success

    mock_client.poll_hourly.side_effect = None
    await refresh(hass, mock_config_entry)

    assert requested_start(mock_client) == recordings.CONNECT_DATE


async def test_a_request_made_during_a_run_waits_for_the_next_one(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The running pass has already polled, so it cannot honour the request."""
    freezer.move_to(NOW)
    await setup(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data

    polling = asyncio.Event()
    resume = asyncio.Event()

    async def slow_poll(*_args: Any) -> Any:
        polling.set()
        await resume.wait()
        return recordings.poll_result()

    mock_client.poll_hourly.side_effect = slow_poll
    running = hass.async_create_task(coordinator.async_refresh())
    await polling.wait()
    assert requested_start(mock_client) == CYCLE_START
    coordinator.request_full(None)
    resume.set()
    await running
    await async_wait_recording_done(hass)

    mock_client.poll_hourly.side_effect = None
    await refresh(hass, mock_config_entry)

    assert requested_start(mock_client) == recordings.CONNECT_DATE


async def test_two_requests_keep_the_older_start(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Whichever reaches furthest back is the one the next run honours."""
    freezer.move_to(NOW)
    await setup(hass, mock_config_entry)

    coordinator = mock_config_entry.runtime_data
    coordinator.request_full(CYCLE_START)
    coordinator.request_full(None)
    await refresh(hass, mock_config_entry)

    assert requested_start(mock_client) == recordings.CONNECT_DATE


async def test_a_floor_pass_starts_at_the_first_cycle_without_a_bill(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A finalized cycle after an unfinalized one does not hide the older one."""
    freezer.move_to(datetime(2026, 11, 5, 12, tzinfo=UTC))
    mock_client.poll_hourly.return_value = recordings.poll_result(
        usage=one_hour_a_day(datetime(2026, 8, 28).date(), datetime(2026, 11, 4).date())
    )
    await setup(hass, mock_config_entry)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_FINALIZE_CYCLE,
        {
            "config_entry_id": mock_config_entry.entry_id,
            "cycle_start": "2026-09-28",
            "read_start": "2026-09-28",
            "read_end": "2026-10-28",
            "service_charge": 33.0,
            "pca_factor": 0.0,
            "tax": 1.5,
            "bill_total": 40.0,
        },
        blocking=True,
    )
    await async_wait_recording_done(hass)

    mock_config_entry.runtime_data.request_full(None)
    await refresh(hass, mock_config_entry)

    assert requested_start(mock_client) == recordings.CONNECT_DATE
    plan = mock_config_entry.runtime_data.last_plan
    assert plan is not None
    assert [one.start for one in plan.cycles] == [
        recordings.CONNECT_DATE,
        CYCLE_START,
        datetime(2026, 10, 28, 4, tzinfo=UTC),
    ]


def test_a_window_cannot_open_after_it_ends() -> None:
    """A pass asked for from a day in a future cycle has nothing to reconcile."""
    with pytest.raises(ValueError, match="after it ends"):
        plan_window(
            recordings.CONNECT_DATE,
            NOW,
            full=FullRequest(from_floor=False, day=NOW + timedelta(days=40)),
            cycle_day=28,
            zone=recordings.PORTAL_ZONE,
        )


async def test_the_reconcile_action_refuses_a_day_still_to_come(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A request the run could never honour would poison every run after it."""
    freezer.move_to(NOW)
    await setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RECONCILE,
            {"config_entry_id": mock_config_entry.entry_id, "from": "2026-10-06"},
            blocking=True,
        )

    await refresh(hass, mock_config_entry)
    assert mock_config_entry.runtime_data.last_update_success
    assert requested_start(mock_client) == CYCLE_START


async def test_a_poll_with_nothing_classified_leaves_stored_costs_alone(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Only a withdrawn usage hour is stale; a silent cost series is not."""
    freezer.move_to(IN_RECORDED_CYCLE)
    await setup(hass, mock_config_entry)
    before = await statistic_sums(
        hass, recordings.COST_STATISTIC_ID, recordings.ON_PEAK_STATISTIC_ID
    )

    mock_client.poll_hourly.return_value = recordings.poll_result(periods={})
    await refresh(hass, mock_config_entry)

    assert len(await statistic_hours(hass, recordings.COST_STATISTIC_ID)) == 72
    assert await statistic_sums(
        hass, recordings.COST_STATISTIC_ID, recordings.ON_PEAK_STATISTIC_ID
    ) == pytest.approx(before)


async def test_a_series_with_no_history_does_not_promote_the_run(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A first cycle the portal never classified leaves cost with nothing to seed from."""
    freezer.move_to(NOW)
    mock_client.poll_hourly.return_value = recordings.poll_result(periods={})
    await setup(hass, mock_config_entry)
    assert await statistic_hours(hass, recordings.COST_STATISTIC_ID) == []

    with caplog.at_level(
        logging.WARNING, "custom_components.nisc_smarthub.coordinator"
    ):
        await refresh(hass, mock_config_entry)

    assert "reconciles the whole history" not in caplog.text
    assert requested_start(mock_client) == CYCLE_START


async def test_a_poll_without_any_classification_raises_the_repair(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    issue_registry: ir.IssueRegistry,
) -> None:
    """With no classified hour to sit between, every hour is a hole."""
    mock_client.poll_hourly.return_value = recordings.poll_result(periods={})

    await setup(hass, mock_config_entry)

    assert len(await statistic_hours(hass, recordings.USAGE_STATISTIC_ID)) == 72
    issue = issue_registry.async_get_issue(
        DOMAIN, f"unclassified_hours_{mock_config_entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["count"] == "72"


async def delete_rows_before(hass: HomeAssistant, moment: datetime) -> None:
    """Drop every stored row of this entry's statistics older than an hour."""

    def drop() -> None:
        with get_instance(hass).get_session() as session:
            metadata = [
                row.id
                for row in session.query(StatisticsMeta.id, StatisticsMeta.source)
                if row.source == DOMAIN
            ]
            session.query(Statistics).filter(
                Statistics.metadata_id.in_(metadata),
                Statistics.start_ts < moment.timestamp(),
            ).delete(synchronize_session=False)
            session.commit()

    await get_instance(hass).async_add_executor_job(drop)


async def test_a_gap_before_the_window_promotes_the_run(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """History removed under the integration's feet comes back by itself."""
    freezer.move_to(NOW)
    await setup(hass, mock_config_entry)
    assert len(await statistic_hours(hass, recordings.USAGE_STATISTIC_ID)) == 72

    await delete_rows_before(hass, CYCLE_START)
    assert await statistic_hours(hass, recordings.USAGE_STATISTIC_ID) == []

    with caplog.at_level(
        logging.WARNING, "custom_components.nisc_smarthub.coordinator"
    ):
        await refresh(hass, mock_config_entry)

    assert "reconciles the whole history" in caplog.text
    assert requested_start(mock_client) == recordings.CONNECT_DATE
    assert mock_client.poll_hourly.call_count == 2
    assert len(await statistic_hours(hass, recordings.USAGE_STATISTIC_ID)) == 72


@dataclass(slots=True)
class HoldRecorder(RecorderTask):
    """Park the recorder thread until released, so queued imports stay queued."""

    gate: threading.Event

    @override
    def run(self, instance: Recorder) -> None:
        """Block the recorder thread on the gate."""
        self.gate.wait()


async def test_the_first_refresh_does_not_wait_on_a_recorder_that_has_not_started(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The recorder parks its queue until startup ends, and setup runs before that.

    Waiting for a commit there would hold startup for a commit that cannot
    happen until startup ends. Once running, every run waits.
    """
    freezer.move_to(NOW)
    recorder = get_instance(hass)
    hass.set_state(CoreState.starting)
    with patch.object(
        recorder, "async_block_till_done", wraps=recorder.async_block_till_done
    ) as barrier:
        await setup(hass, mock_config_entry)
        assert mock_config_entry.runtime_data.last_update_success
        assert barrier.call_count == 0

        hass.set_state(CoreState.running)
        await refresh(hass, mock_config_entry)
        assert barrier.call_count == 1


async def test_a_run_ends_only_once_the_recorder_has_its_rows(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The next run's seed read sees what the last run wrote.

    Imports are queued. With the recorder held back, a run that ended before
    its rows were committed would let the next run find nothing stored before
    its window, promote itself to the floor, and rewrite the history it had
    just written as zeros. Ending the run at the commit is what stops that.
    """
    freezer.move_to(NOW)
    gate = threading.Event()
    get_instance(hass).queue_task(HoldRecorder(gate))
    # Real time, since the frozen clock never reaches a loop timer.
    release = threading.Timer(0.5, gate.set)
    release.start()

    await setup_entry(hass, mock_config_entry)
    assert mock_config_entry.runtime_data.last_update_success
    later = {CYCLE_START: 1.0, CYCLE_START + HOUR: 2.0, CYCLE_START + 2 * HOUR: 3.0}
    mock_client.poll_hourly.return_value = recordings.poll_result(usage=later)
    await mock_config_entry.runtime_data.async_refresh()
    await async_wait_recording_done(hass)
    release.join()

    assert requested_start(mock_client) == CYCLE_START
    assert mock_client.poll_hourly.call_count == 2
    sums = await statistic_sums(hass, recordings.USAGE_STATISTIC_ID)
    assert sums[recordings.USAGE_STATISTIC_ID] == pytest.approx(157.55 + 6.0)
    assert len(await statistic_hours(hass, recordings.USAGE_STATISTIC_ID)) == 75


async def test_the_reconcile_action_runs_a_full_pass_from_a_day(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A day earlier than the connect date is the connect date."""
    freezer.move_to(NOW)
    await setup(hass, mock_config_entry)
    await refresh(hass, mock_config_entry)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RECONCILE,
        {"config_entry_id": mock_config_entry.entry_id, "from": "2026-01-01"},
        blocking=True,
    )
    await async_wait_recording_done(hass)

    assert requested_start(mock_client) == recordings.CONNECT_DATE


async def test_the_reconcile_action_reads_its_day_as_local_midnight(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A day past the floor opens the window at its cycle, in the portal's zone."""
    freezer.move_to(NOW)
    await setup(hass, mock_config_entry)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RECONCILE,
        {"config_entry_id": mock_config_entry.entry_id, "from": "2026-09-30"},
        blocking=True,
    )

    assert requested_start(mock_client) == CYCLE_START


async def test_the_reconcile_action_reports_a_failed_run(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """The caller hears about a portal that would not answer."""
    await setup(hass, mock_config_entry)
    mock_client.poll_hourly.side_effect = ClientError("no route")

    with pytest.raises(Exception, match="no route"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RECONCILE,
            {"config_entry_id": mock_config_entry.entry_id},
            blocking=True,
        )


async def test_the_reconcile_action_refuses_an_entry_it_does_not_own(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """An id from another integration is the caller's mistake, not a crash."""
    await setup(hass, mock_config_entry)
    other = MockConfigEntry(domain="sun")
    other.add_to_hass(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RECONCILE,
            {"config_entry_id": other.entry_id},
            blocking=True,
        )


async def test_the_reconcile_action_refuses_an_unloaded_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """An entry that is not running has no coordinator to ask."""
    await setup(hass, mock_config_entry)
    await hass.config_entries.async_unload(mock_config_entry.entry_id)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RECONCILE,
            {"config_entry_id": mock_config_entry.entry_id},
            blocking=True,
        )


async def test_hours_the_portal_stopped_reporting_are_zeroed_in_every_series(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dropped hour goes to zero in usage, its period, and cost alike."""
    freezer.move_to(IN_RECORDED_CYCLE)
    await setup(hass, mock_config_entry)
    before = await statistic_sums(
        hass, recordings.USAGE_STATISTIC_ID, recordings.COST_STATISTIC_ID
    )

    usage = recordings.hourly_usage()
    dropped = sorted(usage)[-2:]
    for hour in dropped:
        del usage[hour]
    mock_client.poll_hourly.return_value = recordings.poll_result(
        usage=usage, periods=recordings.periods_without(dropped)
    )
    with caplog.at_level(logging.WARNING, "custom_components.nisc_smarthub.writer"):
        await refresh(hass, mock_config_entry)

    states = await statistic_hours(hass, recordings.USAGE_STATISTIC_ID)
    assert len(states) == 72
    assert states[-2:] == [0.0, 0.0]
    assert (await statistic_hours(hass, recordings.COST_STATISTIC_ID))[-2:] == [
        0.0,
        0.0,
    ]
    after = await statistic_sums(
        hass, recordings.USAGE_STATISTIC_ID, recordings.COST_STATISTIC_ID
    )
    assert after[recordings.USAGE_STATISTIC_ID] < before[recordings.USAGE_STATISTIC_ID]
    assert after[recordings.COST_STATISTIC_ID] < before[recordings.COST_STATISTIC_ID]
    assert "the usage series of entry" in caplog.text
    assert "the cost series of entry" in caplog.text

    mock_client.poll_hourly.return_value = recordings.poll_result()
    await refresh(hass, mock_config_entry)

    assert await statistic_sums(
        hass, recordings.USAGE_STATISTIC_ID, recordings.COST_STATISTIC_ID
    ) == pytest.approx(before)


async def test_no_log_line_names_the_account_the_location_or_the_meter(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A revision and a zeroed hour are the chattiest paths, and they stay quiet."""
    freezer.move_to(IN_RECORDED_CYCLE)
    with caplog.at_level(logging.DEBUG, "custom_components.nisc_smarthub"):
        await setup(hass, mock_config_entry)

        usage = recordings.hourly_usage()
        hours = sorted(usage)
        usage[hours[10]] += 1.0
        del usage[hours[20]]
        mock_client.poll_hourly.return_value = recordings.poll_result(
            usage=usage, periods=recordings.periods_without([hours[20]])
        )
        await refresh(hass, mock_config_entry)

    ours = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.nisc_smarthub")
    ]
    assert any("revised" in line for line in ours)
    assert any("no longer reports" in line for line in ours)
    for identifier in (recordings.ACCOUNT, recordings.LOCATION, recordings.METER):
        assert not [line for line in ours if identifier in line]


async def test_a_repair_clears_only_when_a_run_could_see_its_hours(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A routine window that misses the hole cannot say it is gone."""
    freezer.move_to(datetime(2026, 12, 5, 12, tzinfo=UTC))
    hole = sorted(recordings.hourly_usage())[30:33]
    mock_client.poll_hourly.return_value = recordings.poll_result(
        periods=recordings.periods_without(hole)
    )
    await setup(hass, mock_config_entry)
    issue_id = f"unclassified_hours_{mock_config_entry.entry_id}"
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None

    mock_client.poll_hourly.return_value = recordings.poll_result()
    await refresh(hass, mock_config_entry)

    assert requested_start(mock_client) == datetime(2026, 11, 28, 5, tzinfo=UTC)
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None

    mock_config_entry.runtime_data.request_full(None)
    await refresh(hass, mock_config_entry)

    assert requested_start(mock_client) == recordings.CONNECT_DATE
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.parametrize(
    ("dropped", "raises"),
    [
        pytest.param(slice(0, 3), False, id="the_portal_had_not_started_yet"),
        pytest.param(slice(-2, None), False, id="the_classification_lags"),
        pytest.param(slice(30, 33), True, id="a_hole_between_classified_hours"),
    ],
)
async def test_only_an_interior_unclassified_hole_raises_a_repair(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    issue_registry: ir.IssueRegistry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    dropped: slice,
    raises: bool,
) -> None:
    """Leading and trailing hours are the portal's habits, not a fault.

    The clock sits inside the recorded cycle so the clearing run's routine
    window reaches the hole.
    """
    freezer.move_to(IN_RECORDED_CYCLE)
    unclassified = sorted(recordings.hourly_usage())[dropped]
    mock_client.poll_hourly.return_value = recordings.poll_result(
        periods=recordings.periods_without(unclassified)
    )

    with caplog.at_level(logging.INFO, "custom_components.nisc_smarthub.coordinator"):
        await setup(hass, mock_config_entry)

    assert len(await statistic_hours(hass, recordings.USAGE_STATISTIC_ID)) == 72
    assert len(await statistic_hours(hass, recordings.COST_STATISTIC_ID)) == 72 - len(
        unclassified
    )
    issue = issue_registry.async_get_issue(
        DOMAIN, f"unclassified_hours_{mock_config_entry.entry_id}"
    )
    if not raises:
        assert issue is None
        assert "carry kWh and no cost" in caplog.text
        return

    assert issue is not None
    assert issue.translation_placeholders == {
        "count": str(len(unclassified)),
        "oldest": unclassified[0].isoformat(),
    }

    mock_client.poll_hourly.return_value = recordings.poll_result()
    await refresh(hass, mock_config_entry)

    assert (
        issue_registry.async_get_issue(
            DOMAIN, f"unclassified_hours_{mock_config_entry.entry_id}"
        )
        is None
    )
