"""Billing cycle boundaries in the portal's local time.

The calendar puts a boundary at local midnight on the configured day of every
month, with the day clamped to the length of the month, and a cycle runs from
one boundary to the next. A finalized cycle's read dates are boundaries the
utility actually used, so each one replaces the calendar boundary nearest to
it: the cycle after a finalized one runs from its read end to the calendar
boundary after the one that read end replaced, and the cycle before it runs
from the calendar boundary before the one its read start replaced. A read date
a day or two off the calendar therefore moves a boundary rather than adding
one. The first cycle starts at the history floor instead of a boundary,
because the account did not exist before it.
"""

import calendar
from collections.abc import Sequence
from datetime import date, datetime, time, tzinfo
from typing import Any

from .tariff.base import Cycle

FIRST_CYCLE_DAY = 1
LAST_CYCLE_DAY = 31

# How many months of calendar boundaries to lay out on each side of a moment.
# A replaced boundary is always answered for by the read date that replaced
# it, so the boundary before and after any moment sits within two months.
BOUNDARY_REACH = 2


def local_midnight(day: date, zone: tzinfo) -> datetime:
    """Return the instant a calendar day begins in the portal's zone."""
    return datetime.combine(day, time.min, zone)


def cycle_day_from_input(value: Any) -> int:
    """Conform a form's cycle day, or refuse it.

    A number selector hands back a float, and a fractional day of the month
    is a typo, not a day to round.
    """
    try:
        day = float(value)
    except (TypeError, ValueError) as err:
        raise ValueError("the cycle day is not a number") from err
    if not day.is_integer() or not FIRST_CYCLE_DAY <= day <= LAST_CYCLE_DAY:
        raise ValueError(f"the cycle day {value!r} is not a day of the month")
    return int(day)


def cycle_containing(
    moment: datetime,
    *,
    cycle_day: int,
    floor: datetime,
    zone: tzinfo,
    finalized: Sequence[Cycle] = (),
) -> Cycle:
    """Return the cycle that holds a moment.

    A finalized cycle answers for every moment inside it. Any other cycle runs
    between the two boundaries around the moment, and is partial when the
    history floor cuts its start short.
    """
    for frozen in finalized:
        if frozen.start <= moment < frozen.end:
            return frozen

    boundaries = _boundaries(
        moment, cycle_day=cycle_day, zone=zone, finalized=finalized
    )
    start = max(one for one in boundaries if one <= moment)
    end = min(one for one in boundaries if one > moment)
    opens = max(start, floor.astimezone(zone))
    return Cycle(start=opens, end=end, partial=opens != start)


def cycles_covering(
    start: datetime,
    end: datetime,
    *,
    cycle_day: int,
    floor: datetime,
    zone: tzinfo,
    finalized: Sequence[Cycle] = (),
) -> list[Cycle]:
    """Return every cycle a reconcile window touches, oldest first.

    The window's own bounds are not cycle bounds: the first cycle reaches back
    to its own start (or the floor) and the last one runs past `end`, because a
    tariff prices whole cycles.
    """
    covering: list[Cycle] = []
    moment = start
    while moment < end:
        cycle = cycle_containing(
            moment, cycle_day=cycle_day, floor=floor, zone=zone, finalized=finalized
        )
        covering.append(cycle)
        moment = cycle.end
    return covering


def nearest_boundary(read: datetime, *, cycle_day: int, zone: tzinfo) -> datetime:
    """Return the calendar boundary a read date stands in for.

    A tie between two boundaries goes to the earlier one.
    """
    local = read.astimezone(zone)
    candidates = [
        _midnight(*_shift_month(local.year, local.month, delta), cycle_day, zone)
        for delta in (-1, 0, 1)
    ]
    return min(candidates, key=lambda one: (abs(one - local), one))


def _boundaries(
    moment: datetime, *, cycle_day: int, zone: tzinfo, finalized: Sequence[Cycle]
) -> list[datetime]:
    """Return the boundaries around a moment, read dates standing in for the calendar."""
    local = moment.astimezone(zone)
    replaced = {
        nearest_boundary(read, cycle_day=cycle_day, zone=zone)
        for span in finalized
        for read in (span.start, span.end)
    }
    calendar_boundaries = [
        _midnight(*_shift_month(local.year, local.month, delta), cycle_day, zone)
        for delta in range(-BOUNDARY_REACH, BOUNDARY_REACH + 1)
    ]
    kept = [
        boundary
        for boundary in calendar_boundaries
        if boundary not in replaced
        and not any(span.start < boundary < span.end for span in finalized)
    ]
    reads = [read for span in finalized for read in (span.start, span.end)]
    return sorted({*kept, *reads})


def _midnight(year: int, month: int, day: int, zone: tzinfo) -> datetime:
    _, month_length = calendar.monthrange(year, month)
    return datetime(year, month, min(day, month_length), tzinfo=zone)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    years, index = divmod(year * 12 + month - 1 + delta, 12)
    return years, index + 1
