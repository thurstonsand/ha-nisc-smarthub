"""Poll the portal and reconcile what it returns into statistics."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from enum import StrEnum
import logging
from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_LOCATION, CONF_PASSWORD
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ACCOUNT,
    CONF_CYCLE_DAY,
    CONF_POLL_INTERVAL_MINUTES,
    CONF_RATE_VERSIONS,
    CONF_TARIFF,
    CONF_TOTP_SECRET,
    DEFAULT_POLL_INTERVAL_MINUTES,
    DOMAIN,
    ISSUE_NEWER_RATE_VERSION,
    ISSUE_UNCLASSIFIED_HOURS,
    ISSUE_UNKNOWN_PERIOD_LABEL,
)
from .cycles import Cycle, cycle_containing, cycles_covering, local_midnight
from .smarthub import (
    AuthError,
    ClientError,
    PollResult,
    SmartHubClient,
    UnsupportedAccount,
)
from .store import CycleRecord, CycleStore
from .tariff import (
    TARIFFS,
    CostComponents,
    CycleActuals,
    HourUsage,
    RateVersion,
    Tariff,
    rate_version_from_data,
    sum_components,
)
from .writer import COST_KIND, USAGE_KIND, StatisticsWriter, owned_series, period_kind

_LOGGER = logging.getLogger(__name__)

ROUTINE_LOOKBACK: Final = timedelta(days=7)

NO_COST: Final = CostComponents(
    energy=0.0, fixed=0.0, rider=0.0, tax=0.0, adjustment=0.0, residual=0.0
)


@dataclass(frozen=True, kw_only=True)
class SmartHubData:
    """What one reconcile run leaves behind for the entities to read."""

    last_poll: datetime
    newest_data_hour: datetime | None
    meter: str
    cycle: Cycle
    cycle_usage: float
    cycle_cost: CostComponents
    cycle_priced_hours: int
    cycle_unpriced_hours: int
    allowance_remaining: dict[str, float]


@dataclass(frozen=True, kw_only=True)
class Unclassified:
    """Hours carrying usage the portal put in no period, by where they sit.

    The portal classifies nothing before it starts classifying, and it lags the
    usage series at the tail, so only a hole between two classified hours is
    worth a repair.
    """

    before: int
    after: int
    holes: list[datetime]


@dataclass(frozen=True, kw_only=True)
class Reconciliation:
    """One run's statistics and the summary the entities read from it."""

    values: dict[str, dict[datetime, float]]
    data: SmartHubData
    residuals: dict[date, float]
    unknown_labels: set[str]
    unknown_hours: list[datetime]
    unclassified: Unclassified


class WindowTier(StrEnum):
    """How far back a run reaches."""

    ROUTINE = "routine"
    FULL = "full"


@dataclass(frozen=True, kw_only=True)
class WindowPlan:
    """The span of hours one run reconciles, and the cycles it spans."""

    tier: WindowTier
    start: datetime
    end: datetime
    cycles: list[Cycle]


@dataclass(frozen=True, kw_only=True)
class FullRequest:
    """Where a full pass was asked to reach: the floor, a day, or both.

    Asking from the floor means the oldest cycle whose price can still move;
    asking from a day means that day's cycle, finalized or not. A pass asked
    for both ways opens at whichever resolves earlier.
    """

    from_floor: bool
    day: datetime | None

    def merged(self, other: FullRequest) -> FullRequest:
        """Return one request reaching as far as either of two."""
        days = [day for day in (self.day, other.day) if day is not None]
        return FullRequest(
            from_floor=self.from_floor or other.from_floor,
            day=min(days, default=None),
        )


def oldest_unfinalized_start(
    floor: datetime,
    *,
    cycle_day: int,
    zone: tzinfo,
    finalized: Sequence[Cycle],
) -> datetime:
    """Return the start of the first cycle from the floor that has no bill."""
    moment = floor
    while True:
        cycle = cycle_containing(
            moment, cycle_day=cycle_day, floor=floor, zone=zone, finalized=finalized
        )
        if cycle not in finalized:
            return cycle.start
        moment = cycle.end


def plan_window(
    floor: datetime,
    now: datetime,
    *,
    full: FullRequest | None,
    cycle_day: int,
    zone: tzinfo,
    finalized: Sequence[Cycle] = (),
) -> WindowPlan:
    """Return the window this run reconciles.

    A routine run starts at the cycle holding a week ago, which is comfortably
    past the portal's revision horizon. A full pass asked for from the history
    floor starts at the first cycle, walking from the floor, that has no bill;
    a pass asked for from a day starts at that day's cycle, finalized or not,
    which is how a true-up reprices the cycle it was told about. Either way
    the window opens on a cycle boundary, which is what lets the tariff price
    whole cycles, and a finalized cycle inside the window is priced from its
    bill and diffs to nothing.
    """
    if full is None:
        tier = WindowTier.ROUTINE
        moment = now - ROUTINE_LOOKBACK
    else:
        tier = WindowTier.FULL
        candidates: list[datetime] = []
        if full.from_floor:
            candidates.append(
                oldest_unfinalized_start(
                    floor, cycle_day=cycle_day, zone=zone, finalized=finalized
                )
            )
        if full.day is not None:
            candidates.append(max(full.day, floor))
        moment = min(candidates)
    start = cycle_containing(
        moment, cycle_day=cycle_day, floor=floor, zone=zone, finalized=finalized
    ).start
    if start > now:
        raise ValueError(
            f"the window would open at {start.isoformat()}, after it ends at "
            f"{now.isoformat()}"
        )
    return WindowPlan(
        tier=tier,
        start=start,
        end=now,
        cycles=cycles_covering(
            start,
            now,
            cycle_day=cycle_day,
            floor=floor,
            zone=zone,
            finalized=finalized,
        ),
    )


class NiscSmartHubCoordinator(DataUpdateCoordinator[SmartHubData]):
    """Logs in, polls one service location, and writes its statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: SmartHubClient,
        local_zone: tzinfo,
        store: CycleStore,
    ) -> None:
        """Initialize the coordinator for one config entry."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=entry.options.get(
                    CONF_POLL_INTERVAL_MINUTES, DEFAULT_POLL_INTERVAL_MINUTES
                )
            ),
        )
        self._client = client
        self._local_zone = local_zone
        self._store = store
        self._email: str = entry.data[CONF_EMAIL]
        self._password: str = entry.data[CONF_PASSWORD]
        self._totp_secret: str = entry.data[CONF_TOTP_SECRET]
        self._account: str = entry.data[CONF_ACCOUNT]
        self._location: str = entry.data[CONF_LOCATION]
        self._cycle_day: int = entry.data[CONF_CYCLE_DAY]
        self._tariff: Tariff = TARIFFS[entry.data[CONF_TARIFF]]
        self._versions: list[RateVersion] = [
            rate_version_from_data(one) for one in entry.data[CONF_RATE_VERSIONS]
        ]
        self._writer = StatisticsWriter(
            hass,
            entry_id=entry.entry_id,
            series=owned_series(
                account=self._account,
                location=self._location,
                service_description=entry.title,
                periods=self._tariff.periods,
            ),
        )
        # A bill saved but never priced is a run that failed after the save,
        # and a fresh coordinator would otherwise skip that cycle as finalized.
        self._pending: FullRequest | None = FullRequest(
            from_floor=True, day=store.earliest_unpriced_start()
        )
        # The oldest hour each repair was raised for. A run can only clear a
        # repair when its window reaches back that far, because a shorter
        # window cannot see whether the hour is still wrong.
        self._oldest_unknown_hour: datetime | None = None
        self._oldest_hole: datetime | None = None
        self._floor: datetime | None = None
        self.last_plan: WindowPlan | None = None

    @property
    def statistic_ids(self) -> list[str]:
        """Return every statistic id this entry owns."""
        return [one.statistic_id for one in self._writer.series]

    @property
    def tariff(self) -> Tariff:
        """Return the tariff this entry is priced under."""
        return self._tariff

    @property
    def cycle_records(self) -> dict[date, CycleRecord]:
        """Return what every posted bill said, by the cycle it settled."""
        return self._store.records

    def cycle_starting(self, day: date) -> Cycle | None:
        """Return the cycle that begins on a local day, if one does.

        The history floor comes from the portal, so this answers only once a
        run has read it, which a loaded entry guarantees.
        """
        assert self._floor is not None
        midnight = local_midnight(day, self._local_zone)
        cycle = cycle_containing(
            midnight,
            cycle_day=self._cycle_day,
            floor=self._floor,
            zone=self._local_zone,
            finalized=self._store.spans(),
        )
        return cycle if cycle.start == midnight else None

    def overlapping_record(
        self, day: date, read_start: datetime, read_end: datetime
    ) -> date | None:
        """Return the day of another finalized cycle a span would overlap, if any."""
        for other, record in self._store.records.items():
            if other == day:
                continue
            if (
                record.actuals.read_start < read_end
                and read_start < record.actuals.read_end
            ):
                return other
        return None

    async def async_finalize_cycle(self, day: date, actuals: CycleActuals) -> None:
        """Record a posted bill and ask for the pass that prices the cycle to it."""
        await self._store.async_finalize(day, actuals)
        self._request_before(day, actuals.read_start)

    async def async_unfinalize_cycle(self, day: date) -> None:
        """Forget a posted bill and ask for the pass that re-estimates the cycle."""
        actuals = self._store.actuals_for(day)
        assert actuals is not None
        await self._store.async_unfinalize(day)
        self._request_before(day, actuals.read_start)

    def _request_before(self, day: date, read_start: datetime) -> None:
        """Ask for a pass from the cycle before the one a bill opens.

        A read date that moved also moved the end of the cycle before it, so
        that cycle has to be repriced as well. The hour before the earlier of
        the cycle's day and its read start lies in it.
        """
        opens = min(local_midnight(day, self._local_zone), read_start)
        self.request_full(opens - timedelta(hours=1))

    def request_full(self, from_: datetime | None) -> None:
        """Ask the next run to reconcile from `from_`, or the history floor.

        The request is taken by the next run to start and handed back if that
        run fails. A request made while a run is in flight waits for the run
        after it, since the running one has already polled.
        """
        requested = FullRequest(from_floor=from_ is None, day=from_)
        if self._pending is not None:
            requested = self._pending.merged(requested)
        self._pending = requested

    async def _async_update_data(self) -> SmartHubData:
        requested = self._pending
        self._pending = None
        try:
            return await self._async_run(requested)
        except BaseException:
            if requested is not None:
                self._pending = (
                    requested
                    if self._pending is None
                    else self._pending.merged(requested)
                )
            raise

    async def _async_run(self, requested: FullRequest | None) -> SmartHubData:
        try:
            # The portal's token lives 299 seconds, so a run authenticates
            # rather than holding one between updates.
            await self._client.login(self._email, self._password, self._totp_secret)
            floor = (await self._client.billing(self._account)).connect_date
            self._floor = floor
            now = dt_util.utcnow()
            plan = self._plan(floor, now, requested)
            seeds = await self._writer.async_read_seeds(
                window_start=plan.start, floor=floor
            )
            if seeds is None:
                _LOGGER.warning(
                    "nothing is stored between %s and %s, so this run reconciles "
                    "the whole history",
                    floor.isoformat(),
                    plan.start.isoformat(),
                )
                plan = self._plan(floor, now, FullRequest(from_floor=False, day=floor))
                seeds = dict.fromkeys(self.statistic_ids, 0.0)
            poll = await self._client.poll_hourly(
                self._account, self._location, plan.start, now
            )
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except UnsupportedAccount as err:
            raise ConfigEntryError(str(err)) from err
        except ClientError as err:
            raise UpdateFailed(str(err)) from err

        run = self._reconcile(poll, plan=plan, floor=floor, now=now)
        await self._writer.async_write(run.values, seeds, window_start=plan.start)
        # Imports are queued, not committed; the next run's reads have to see
        # these rows, so the run ends when the recorder has written them. The
        # recorder thread does not touch its queue until Home Assistant has
        # started, and the first refresh runs during setup, before that, so
        # waiting there would hold startup for a commit that cannot happen.
        # That queue drains at startup, ahead of anything a later run asks for.
        if self.hass.state is CoreState.running:
            await get_instance(self.hass).async_block_till_done()
        # A stored residual says the cycle's rows match its bill, so it is
        # stored only once they do.
        await self._store.async_set_residuals(run.residuals)
        self._report(run, plan)
        return run.data

    def _plan(
        self, floor: datetime, now: datetime, full: FullRequest | None
    ) -> WindowPlan:
        plan = plan_window(
            floor,
            now,
            full=full,
            cycle_day=self._cycle_day,
            zone=self._local_zone,
            finalized=self._store.spans(),
        )
        _LOGGER.debug(
            "reconciling a %s window, %s to %s, across %d cycles",
            plan.tier,
            plan.start.isoformat(),
            plan.end.isoformat(),
            len(plan.cycles),
        )
        self.last_plan = plan
        return plan

    def _reconcile(
        self, poll: PollResult, *, plan: WindowPlan, floor: datetime, now: datetime
    ) -> Reconciliation:
        """Turn one poll into the statistics it implies and a run summary."""
        classified, unknown_labels, unknown_hours = self._split(poll)
        hours = _hours(poll, classified)

        costs: dict[datetime, float] = {}
        finalized = self._store.spans()
        actuals_by_start = self._store.actuals_by_start()
        days_by_start = self._store.days_by_start()
        current = cycle_containing(
            now,
            cycle_day=self._cycle_day,
            floor=floor,
            zone=self._local_zone,
            finalized=finalized,
        )
        breakdown = NO_COST
        allowance: dict[str, float] = {}
        residuals: dict[date, float] = {}
        priced_hours = 0
        unpriced_hours = 0
        for cycle in plan.cycles:
            within = [hour for hour in hours if cycle.start <= hour.start < cycle.end]
            actuals = actuals_by_start.get(cycle.start)
            priced = self._tariff.price_cycle(cycle, within, self._versions, actuals)
            costs.update({cost.start: cost.total for cost in priced})
            if actuals is not None:
                residuals[days_by_start[cycle.start]] = sum(
                    cost.residual for cost in priced
                )
            if cycle.start == current.start:
                breakdown = sum_components(priced)
                priced_hours = len(priced)
                unpriced_hours = len(within) - len(priced)
                allowance = self._tariff.allowance_remaining(
                    cycle, within, self._versions
                )

        values = {self._writer.by_kind(USAGE_KIND).statistic_id: poll.usage}
        if len(self._tariff.periods) > 1:
            for period in self._tariff.periods:
                series = self._writer.by_kind(period_kind(period.slug))
                values[series.statistic_id] = classified.get(period.slug, {})
        values[self._writer.by_kind(COST_KIND).statistic_id] = costs

        return Reconciliation(
            values=values,
            residuals=residuals,
            data=SmartHubData(
                last_poll=now,
                newest_data_hour=max(poll.usage, default=None),
                meter=poll.meter,
                cycle=current,
                cycle_usage=sum(
                    value
                    for hour, value in poll.usage.items()
                    if current.start <= hour < current.end
                ),
                cycle_cost=breakdown,
                cycle_priced_hours=priced_hours,
                cycle_unpriced_hours=unpriced_hours,
                allowance_remaining=allowance,
            ),
            unknown_labels=unknown_labels,
            unknown_hours=unknown_hours,
            unclassified=_unclassified(hours, set(unknown_hours)),
        )

    def _split(
        self, poll: PollResult
    ) -> tuple[dict[str, dict[datetime, float]], set[str], list[datetime]]:
        classified: dict[str, dict[datetime, float]] = {}
        unknown_labels: set[str] = set()
        unknown_hours: set[datetime] = set()
        for label, values in poll.periods.items():
            slug = self._tariff.slug_for_label(label)
            if slug is None:
                unknown_labels.add(label)
                unknown_hours.update(values)
                continue
            classified[slug] = values
        return classified, unknown_labels, sorted(unknown_hours)

    def _report(self, run: Reconciliation, plan: WindowPlan) -> None:
        """Raise or clear the repair issues this run's hours call for."""
        entry = self.config_entry
        assert entry is not None
        if run.unknown_labels:
            _LOGGER.warning(
                "the portal classified hours as %s, which this tariff does not price",
                ", ".join(sorted(run.unknown_labels)),
            )
            self._oldest_unknown_hour = _oldest(
                self._oldest_unknown_hour, run.unknown_hours[0]
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_UNKNOWN_PERIOD_LABEL}_{entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_UNKNOWN_PERIOD_LABEL,
                translation_placeholders={
                    "labels": ", ".join(sorted(run.unknown_labels)),
                    "tariff": self._tariff.display_name,
                },
            )
        else:
            self._oldest_unknown_hour = self._clear_if_seen(
                ISSUE_UNKNOWN_PERIOD_LABEL, self._oldest_unknown_hour, plan.start
            )

        if run.unclassified.before or run.unclassified.after:
            _LOGGER.info(
                "%d hours before the portal's first classified hour and %d at the "
                "tail it has not classified yet carry kWh and no cost",
                run.unclassified.before,
                run.unclassified.after,
            )

        if run.unclassified.holes:
            _LOGGER.warning(
                "%d hours between classified hours carry usage the portal never "
                "classified, the oldest at %s, so they have kWh and no cost",
                len(run.unclassified.holes),
                run.unclassified.holes[0].isoformat(),
            )
            self._oldest_hole = _oldest(self._oldest_hole, run.unclassified.holes[0])
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_UNCLASSIFIED_HOURS}_{entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_UNCLASSIFIED_HOURS,
                translation_placeholders={
                    "count": str(len(run.unclassified.holes)),
                    "oldest": run.unclassified.holes[0].isoformat(),
                },
            )
        else:
            self._oldest_hole = self._clear_if_seen(
                ISSUE_UNCLASSIFIED_HOURS, self._oldest_hole, plan.start
            )

        self._report_published_versions(entry)

    def _clear_if_seen(
        self, issue: str, oldest: datetime | None, window_start: datetime
    ) -> datetime | None:
        """Clear a repair the run found no cause for, if it could have seen one.

        Returns what the coordinator should keep remembering: the hour, when
        the window never reached it, or nothing once the issue is gone.
        """
        if oldest is not None and window_start > oldest:
            return oldest
        entry = self.config_entry
        assert entry is not None
        ir.async_delete_issue(self.hass, DOMAIN, f"{issue}_{entry.entry_id}")
        return None

    def _report_published_versions(self, entry: ConfigEntry) -> None:
        """Offer the newest version this release ships, if it is newer.

        A published version is never applied on its own: a rate the member is
        not billed under would silently rewrite their history.
        """
        published = self._tariff.newest_published_version()
        configured = max(one.effective_from for one in self._versions)
        if published.effective_from <= configured:
            ir.async_delete_issue(
                self.hass, DOMAIN, f"{ISSUE_NEWER_RATE_VERSION}_{entry.entry_id}"
            )
            return

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_NEWER_RATE_VERSION}_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_NEWER_RATE_VERSION,
            translation_placeholders={
                "effective_from": published.effective_from.isoformat(),
                "tariff": self._tariff.display_name,
            },
        )


def _oldest(remembered: datetime | None, found: datetime) -> datetime:
    return found if remembered is None else min(remembered, found)


def _hours(
    poll: PollResult, classified: dict[str, dict[datetime, float]]
) -> list[HourUsage]:
    return [
        HourUsage(
            start=start,
            total=total,
            by_period={
                slug: values[start]
                for slug, values in classified.items()
                if start in values
            },
        )
        for start, total in poll.usage.items()
    ]


def _unclassified(
    hours: Sequence[HourUsage], unknown_hours: set[datetime]
) -> Unclassified:
    """Sort the hours nobody classified into floor, tail, and holes."""
    missing = sorted(
        hour.start
        for hour in hours
        if not hour.by_period and hour.start not in unknown_hours
    )
    known = [hour.start for hour in hours if hour.by_period]
    if not known:
        return Unclassified(before=0, after=0, holes=missing)

    first, last = min(known), max(known)
    return Unclassified(
        before=sum(1 for hour in missing if hour < first),
        after=sum(1 for hour in missing if hour > last),
        holes=[hour for hour in missing if first < hour < last],
    )
