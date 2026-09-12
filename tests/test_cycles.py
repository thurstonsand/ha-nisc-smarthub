"""Billing cycle boundaries, in the portal's local time."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.nisc_smarthub.cycles import (
    cycle_containing,
    cycle_day_from_input,
    cycles_covering,
    nearest_boundary,
)
from custom_components.nisc_smarthub.tariff import Cycle

ZONE = ZoneInfo("America/New_York")
FLOOR = datetime(2026, 8, 28, 4, tzinfo=UTC)
OLD_FLOOR = datetime(2020, 1, 1, 5, tzinfo=UTC)


def finalized(start: datetime, end: datetime) -> Cycle:
    """Return a finalized cycle between two local midnights."""
    return Cycle(start=start, end=end, partial=False)


def bounds(cycles: list[Cycle]) -> list[tuple[datetime, datetime]]:
    """Return each cycle's start and end."""
    return [(one.start, one.end) for one in cycles]


def test_a_cycle_runs_from_local_midnight_to_local_midnight() -> None:
    """The first of the month to the first of the next, in local time."""
    cycle = cycle_containing(
        datetime(2026, 10, 5, 12, tzinfo=UTC),
        cycle_day=1,
        floor=OLD_FLOOR,
        zone=ZONE,
    )

    assert cycle.start == datetime(2026, 10, 1, tzinfo=ZONE)
    assert cycle.end == datetime(2026, 11, 1, tzinfo=ZONE)
    assert cycle.partial is False


def test_a_moment_before_the_cycle_day_belongs_to_the_previous_month() -> None:
    """The fifth is still in the cycle that started on the fifteenth."""
    cycle = cycle_containing(
        datetime(2026, 10, 5, 12, tzinfo=UTC),
        cycle_day=15,
        floor=OLD_FLOOR,
        zone=ZONE,
    )

    assert cycle.start == datetime(2026, 9, 15, tzinfo=ZONE)
    assert cycle.end == datetime(2026, 10, 15, tzinfo=ZONE)


def test_the_cycle_day_clamps_to_a_short_month() -> None:
    """Day 31 falls on the last day of a month that has no 31st."""
    cycle = cycle_containing(
        datetime(2026, 10, 2, 12, tzinfo=UTC),
        cycle_day=31,
        floor=OLD_FLOOR,
        zone=ZONE,
    )

    assert cycle.start == datetime(2026, 9, 30, tzinfo=ZONE)
    assert cycle.end == datetime(2026, 10, 31, tzinfo=ZONE)


def test_a_cycle_crosses_the_year() -> None:
    """December's cycle ends in January, and January's starts in December."""
    december = cycle_containing(
        datetime(2026, 12, 20, tzinfo=UTC), cycle_day=15, floor=OLD_FLOOR, zone=ZONE
    )
    january = cycle_containing(
        datetime(2027, 1, 5, tzinfo=UTC), cycle_day=15, floor=OLD_FLOOR, zone=ZONE
    )

    assert december.start == datetime(2026, 12, 15, tzinfo=ZONE)
    assert december.end == datetime(2027, 1, 15, tzinfo=ZONE)
    assert january.start == december.start
    assert january.end == december.end


def test_the_first_cycle_starts_at_the_history_floor() -> None:
    """Nothing was metered before the connect date, so the cycle starts there."""
    cycle = cycle_containing(
        datetime(2026, 8, 30, 12, tzinfo=UTC), cycle_day=1, floor=FLOOR, zone=ZONE
    )

    assert cycle.start == FLOOR
    assert cycle.end == datetime(2026, 9, 1, tzinfo=ZONE)
    assert cycle.partial is True


def test_a_cycle_after_the_first_is_not_partial() -> None:
    """The floor stops mattering once a whole cycle sits after it."""
    cycle = cycle_containing(
        datetime(2026, 9, 15, 12, tzinfo=UTC), cycle_day=1, floor=FLOOR, zone=ZONE
    )

    assert cycle.start == datetime(2026, 9, 1, tzinfo=ZONE)
    assert cycle.partial is False


def test_a_window_inside_one_cycle_covers_that_cycle() -> None:
    """A short window still hands the tariff the whole cycle around it."""
    covering = cycles_covering(
        datetime(2026, 9, 20, tzinfo=UTC),
        datetime(2026, 9, 25, tzinfo=UTC),
        cycle_day=1,
        floor=OLD_FLOOR,
        zone=ZONE,
    )

    assert [one.start for one in covering] == [datetime(2026, 9, 1, tzinfo=ZONE)]
    assert covering[0].end == datetime(2026, 10, 1, tzinfo=ZONE)


def test_a_window_spanning_a_boundary_covers_both_cycles() -> None:
    """Each cycle is priced on its own, so the window splits at the boundary."""
    covering = cycles_covering(
        datetime(2026, 9, 20, tzinfo=UTC),
        datetime(2026, 11, 2, tzinfo=UTC),
        cycle_day=1,
        floor=OLD_FLOOR,
        zone=ZONE,
    )

    assert [one.start for one in covering] == [
        datetime(2026, 9, 1, tzinfo=ZONE),
        datetime(2026, 10, 1, tzinfo=ZONE),
        datetime(2026, 11, 1, tzinfo=ZONE),
    ]


def test_the_first_covered_cycle_starts_at_the_floor() -> None:
    """History begins at the connect date, mid-cycle or not."""
    covering = cycles_covering(
        FLOOR,
        datetime(2026, 9, 15, tzinfo=UTC),
        cycle_day=1,
        floor=FLOOR,
        zone=ZONE,
    )

    assert covering[0].start == FLOOR
    assert covering[0].partial is True
    assert covering[1].start == datetime(2026, 9, 1, tzinfo=ZONE)


@pytest.mark.parametrize(
    ("cycle_day", "read_end", "expected"),
    [
        pytest.param(
            28,
            datetime(2026, 9, 25, tzinfo=ZONE),
            [
                (FLOOR, datetime(2026, 9, 25, tzinfo=ZONE)),
                (
                    datetime(2026, 9, 25, tzinfo=ZONE),
                    datetime(2026, 10, 28, tzinfo=ZONE),
                ),
                (
                    datetime(2026, 10, 28, tzinfo=ZONE),
                    datetime(2026, 11, 28, tzinfo=ZONE),
                ),
            ],
            id="a_read_date_early",
        ),
        pytest.param(
            28,
            datetime(2026, 9, 30, tzinfo=ZONE),
            [
                (FLOOR, datetime(2026, 9, 30, tzinfo=ZONE)),
                (
                    datetime(2026, 9, 30, tzinfo=ZONE),
                    datetime(2026, 10, 28, tzinfo=ZONE),
                ),
                (
                    datetime(2026, 10, 28, tzinfo=ZONE),
                    datetime(2026, 11, 28, tzinfo=ZONE),
                ),
            ],
            id="a_read_date_late",
        ),
        pytest.param(
            31,
            datetime(2026, 10, 1, tzinfo=ZONE),
            [
                (FLOOR, datetime(2026, 10, 1, tzinfo=ZONE)),
                (
                    datetime(2026, 10, 1, tzinfo=ZONE),
                    datetime(2026, 10, 31, tzinfo=ZONE),
                ),
                (
                    datetime(2026, 10, 31, tzinfo=ZONE),
                    datetime(2026, 11, 30, tzinfo=ZONE),
                ),
            ],
            id="a_read_date_past_the_clamped_day",
        ),
    ],
)
def test_a_read_date_replaces_the_nearest_calendar_boundary(
    cycle_day: int, read_end: datetime, expected: list[tuple[datetime, datetime]]
) -> None:
    """A read date a few days off the calendar moves a boundary, never adds one."""
    covering = cycles_covering(
        FLOOR,
        datetime(2026, 11, 5, tzinfo=UTC),
        cycle_day=cycle_day,
        floor=FLOOR,
        zone=ZONE,
        finalized=[finalized(FLOOR, read_end)],
    )

    assert bounds(covering) == expected


def test_a_finalized_cycle_swallows_the_calendar_boundaries_inside_it() -> None:
    """A bill spanning two calendar cycles is one cycle, and the next starts after it."""
    read_end = datetime(2026, 10, 20, tzinfo=ZONE)
    covering = cycles_covering(
        FLOOR,
        datetime(2026, 11, 5, tzinfo=UTC),
        cycle_day=28,
        floor=FLOOR,
        zone=ZONE,
        finalized=[finalized(FLOOR, read_end)],
    )

    assert bounds(covering) == [
        (FLOOR, read_end),
        (read_end, datetime(2026, 11, 28, tzinfo=ZONE)),
    ]


def test_the_cycle_before_a_finalized_one_ends_at_its_read_start() -> None:
    """An early read start shortens the cycle before it rather than adding a sliver."""
    read_start = datetime(2026, 9, 26, tzinfo=ZONE)
    covering = cycles_covering(
        FLOOR,
        datetime(2026, 11, 5, tzinfo=UTC),
        cycle_day=28,
        floor=FLOOR,
        zone=ZONE,
        finalized=[finalized(read_start, datetime(2026, 10, 28, tzinfo=ZONE))],
    )

    assert bounds(covering) == [
        (FLOOR, read_start),
        (read_start, datetime(2026, 10, 28, tzinfo=ZONE)),
        (datetime(2026, 10, 28, tzinfo=ZONE), datetime(2026, 11, 28, tzinfo=ZONE)),
    ]


def test_a_tie_between_two_boundaries_goes_to_the_earlier() -> None:
    """The 15th of a 30-day month sits 15 days from both boundaries of day 1."""
    boundary = nearest_boundary(
        datetime(2026, 9, 16, tzinfo=ZONE), cycle_day=1, zone=ZONE
    )

    assert boundary == datetime(2026, 9, 1, tzinfo=ZONE)


def test_an_empty_window_covers_nothing() -> None:
    """A window that ends where it starts has no cycle to price."""
    assert cycles_covering(FLOOR, FLOOR, cycle_day=1, floor=FLOOR, zone=ZONE) == []


@pytest.mark.parametrize("value", [28, 28.0, "28"], ids=["int", "float", "text"])
def test_a_whole_day_of_the_month_is_a_cycle_day(value: object) -> None:
    """A number selector hands back a float, and it arrives as the int it means."""
    assert cycle_day_from_input(value) == 28


@pytest.mark.parametrize(
    "value",
    [0, 32, 28.5, "half", None],
    ids=["zero", "past_31", "half", "text", "none"],
)
def test_anything_else_is_not_a_cycle_day(value: object) -> None:
    """A fractional or out-of-range day is refused rather than rounded or clamped."""
    with pytest.raises(ValueError, match="cycle day"):
        cycle_day_from_input(value)
