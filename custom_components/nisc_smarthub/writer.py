"""Reconcile the portal's hours into the recorder's external statistics.

Every write ends at the newest hour the poll returned, so a rewritten hour
carries its correction through the stored tail instead of leaving a
compensating error at the first row nobody touched.

Logs name a series by its kind and the entry that owns it, never by its
statistic id: the id spells out the account number and the service location.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import logging

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance

from .const import CURRENCY_UNIT, DOMAIN
from .tariff import Period

_LOGGER = logging.getLogger(__name__)

ENERGY_TOLERANCE = 1e-9
CURRENCY_TOLERANCE = 1e-6

USAGE_KIND = "usage"
COST_KIND = "cost"


@dataclass(frozen=True, kw_only=True)
class Series:
    """One statistic this entry owns: its id, its metadata, and how logs name it."""

    kind: str
    statistic_id: str
    name: str
    unit_of_measurement: str
    unit_class: str | None
    tolerance: float


def period_kind(slug: str) -> str:
    """Return the kind of one period's usage series."""
    return f"{USAGE_KIND}_{slug}"


def owned_series(
    *,
    account: str,
    location: str,
    service_description: str,
    periods: Sequence[Period],
) -> list[Series]:
    """Return every statistic a service location owns under a tariff.

    The per-period series exist only when the tariff splits usage at all, and
    then every period has one, whether or not a window holds hours for it.
    Currency has no unit class in the recorder's conversion tables, so cost
    rows are stored in the unit they were priced in and never converted.
    """
    prefix = f"{DOMAIN}:{account}_{location}"
    series = [
        Series(
            kind=USAGE_KIND,
            statistic_id=f"{prefix}_usage",
            name=f"{service_description} usage",
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            unit_class="energy",
            tolerance=ENERGY_TOLERANCE,
        )
    ]
    if len(periods) > 1:
        series += [
            Series(
                kind=period_kind(period.slug),
                statistic_id=f"{prefix}_usage_{period.slug}",
                name=f"{service_description} {period.label.lower()} usage",
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                unit_class="energy",
                tolerance=ENERGY_TOLERANCE,
            )
            for period in periods
        ]
    series.append(
        Series(
            kind=COST_KIND,
            statistic_id=f"{prefix}_cost",
            name=f"{service_description} cost",
            unit_of_measurement=CURRENCY_UNIT,
            unit_class=None,
            tolerance=CURRENCY_TOLERANCE,
        )
    )
    return series


@dataclass(frozen=True, kw_only=True)
class Row:
    """One hour of a statistic: its own value and the running total."""

    hour: datetime
    state: float
    total: float

    def as_statistic(self) -> StatisticData:
        """Return the row in the shape the recorder imports."""
        return StatisticData(start=self.hour, state=self.state, sum=self.total)


class StatisticsWriter:
    """Owns a fixed set of statistics, from their ids to their cumulative sums."""

    def __init__(
        self, hass: HomeAssistant, *, entry_id: str, series: Sequence[Series]
    ) -> None:
        """Initialize the writer for the series one entry owns."""
        self._hass = hass
        self._entry_id = entry_id
        self._series = list(series)
        kinds = [one.kind for one in self._series]
        if len(set(kinds)) != len(kinds):
            raise ValueError(f"series kinds must be unique, got {kinds}")

    @property
    def series(self) -> list[Series]:
        """Return every series this writer owns, in write order."""
        return list(self._series)

    def by_kind(self, kind: str) -> Series:
        """Return the owned series of one kind."""
        for one in self._series:
            if one.kind == kind:
                return one
        raise KeyError(kind)

    async def async_read_seeds(
        self, *, window_start: datetime, floor: datetime
    ) -> dict[str, float] | None:
        """Return the cumulative sum each series carries into the window.

        `None` says the usage history between the floor and the window is
        missing, so the run cannot seed honestly and the caller reconciles from
        the floor. Any other series with nothing stored before the window has
        simply had nothing to say yet, and starts from zero.
        """
        if window_start <= floor:
            return dict.fromkeys((one.statistic_id for one in self._series), 0.0)

        stored = await self._async_read(floor, window_start)
        seeds: dict[str, float] = {}
        for one in self._series:
            rows = stored.get(one.statistic_id)
            if not rows:
                if one.kind == USAGE_KIND:
                    return None
                seeds[one.statistic_id] = 0.0
                continue
            seed = rows[-1].get("sum")
            if seed is None:
                return None
            seeds[one.statistic_id] = seed
        return seeds

    async def async_write(
        self,
        values: dict[str, dict[datetime, float]],
        seeds: dict[str, float],
        *,
        window_start: datetime,
    ) -> None:
        """Write every row from the window start on that differs from what is stored.

        The usage series decides which stored hours the portal has withdrawn:
        every stored usage hour in the window the poll lacks. Such an hour
        cannot be deleted, so every series rewrites it to zero with the running
        total carried through it, which keeps the series continuous and the
        recorder's derived change for that hour at zero. An hour the portal
        still reports but that one series has no value for, an unclassified
        hour's cost for instance, is not withdrawn: that series writes no row
        for it and leaves whatever it stored alone. Hours the portal never
        reported get no row at all.
        """
        stored = await self._async_read(window_start, None)
        usage = self.by_kind(USAGE_KIND)
        stale = stale_hours(
            values[usage.statistic_id],
            _states_of(stored.get(usage.statistic_id, [])),
            window_start=window_start,
        )
        for one in self._series:
            existing = stored.get(one.statistic_id, [])
            states = _states_of(existing)
            rows = build_rows(
                values[one.statistic_id],
                seeds[one.statistic_id],
                window_start=window_start,
                stale=stale,
                stored=states,
            )
            revised, changed = self._revisions(one, rows, existing)
            self._report_stale(one, stale, revised, states)
            if not revised:
                _LOGGER.debug("%s is already up to date", self._label(one))
                continue
            if changed:
                _LOGGER.info(
                    "%s revised %d hours between %s and %s",
                    self._label(one),
                    len(changed),
                    changed[0].isoformat(),
                    changed[-1].isoformat(),
                )
            async_add_external_statistics(self._hass, _metadata(one), revised)

    def _label(self, series: Series) -> str:
        return f"the {series.kind} series of entry {self._entry_id}"

    def _report_stale(
        self,
        series: Series,
        stale: list[datetime],
        revised: list[StatisticData],
        states: Mapping[datetime, float],
    ) -> None:
        """Warn on the run that zeroes an hour; a zero row carried along is routine."""
        if not stale:
            return
        rewritten = {row["start"] for row in revised}
        zeroed = [hour for hour in stale if states.get(hour) != 0.0]
        log = _LOGGER.warning if rewritten.intersection(zeroed) else _LOGGER.debug
        log(
            "%s holds %d hours from %s to %s the portal no longer reports; they "
            "are written as zero so the series stays continuous",
            self._label(series),
            len(stale),
            stale[0].isoformat(),
            stale[-1].isoformat(),
        )

    def _revisions(
        self, series: Series, rows: list[Row], stored: list[StatisticsRow]
    ) -> tuple[list[StatisticData], list[datetime]]:
        """Return the rows to import, and the hours whose own value changed."""
        by_start = {row.get("start"): row for row in stored}
        revised: list[StatisticData] = []
        changed: list[datetime] = []
        for row in rows:
            before = by_start.get(row.hour.timestamp())
            if before is None:
                revised.append(row.as_statistic())
                continue
            state_changed = _differs(before.get("state"), row.state, series.tolerance)
            if not state_changed and not _differs(
                before.get("sum"), row.total, series.tolerance
            ):
                continue
            if state_changed:
                changed.append(row.hour)
                _LOGGER.debug(
                    "%s revised %s from %s to %s %s",
                    self._label(series),
                    row.hour.isoformat(),
                    before.get("state"),
                    row.state,
                    series.unit_of_measurement,
                )
            revised.append(row.as_statistic())
        return revised, changed

    async def _async_read(
        self, start: datetime, end: datetime | None
    ) -> dict[str, list[StatisticsRow]]:
        return await get_instance(self._hass).async_add_executor_job(
            statistics_during_period,
            self._hass,
            start,
            end,
            {one.statistic_id for one in self._series},
            "hour",
            None,
            {"state", "sum"},
        )


def stale_hours(
    usage: Mapping[datetime, float],
    stored: Mapping[datetime, float],
    *,
    window_start: datetime,
) -> list[datetime]:
    """Return the stored usage hours in the window the poll no longer carries."""
    return sorted(hour for hour in stored if hour >= window_start and hour not in usage)


def build_rows(
    values: Mapping[datetime, float],
    seed: float,
    *,
    window_start: datetime,
    stale: Sequence[datetime],
    stored: Mapping[datetime, float],
) -> list[Row]:
    """Return the window's cumulative rows in order.

    Every hour with a value gets a row carrying it; every stale hour gets a
    zero row. A stored hour that is neither keeps its row, and its stored
    value stays in the running total so the rows after it continue from it.
    """
    kept = {hour for hour in stored if hour >= window_start}
    total = seed
    rows: list[Row] = []
    for hour in sorted({*values, *stale, *kept}):
        if hour < window_start:
            continue
        if hour in stale:
            state = 0.0
        elif hour in values:
            state = values[hour]
        else:
            total += stored[hour]
            continue
        total += state
        rows.append(Row(hour=hour, state=state, total=total))
    return rows


def _states_of(stored: list[StatisticsRow]) -> dict[datetime, float]:
    states: dict[datetime, float] = {}
    for row in stored:
        start = row.get("start")
        state = row.get("state")
        assert start is not None
        assert state is not None
        states[datetime.fromtimestamp(start, UTC)] = state
    return states


def _differs(before: float | None, after: float, tolerance: float) -> bool:
    if before is None:
        return True
    return abs(before - after) > tolerance


def _metadata(series: Series) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=series.name,
        source=DOMAIN,
        statistic_id=series.statistic_id,
        unit_class=series.unit_class,
        unit_of_measurement=series.unit_of_measurement,
    )
