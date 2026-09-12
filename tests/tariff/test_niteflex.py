"""NiteFlex, priced a whole cycle at a time."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.nisc_smarthub.tariff import (
    NITEFLEX,
    Cycle,
    CycleActuals,
    HourCost,
    HourUsage,
    RateVersion,
    nominal_hours,
    rate_version_from_data,
    rate_version_to_data,
    tariff_for_codes,
)
from custom_components.nisc_smarthub.tariff.niteflex import (
    OFF_PEAK,
    ON_PEAK,
    SUPER_OFF_PEAK,
)

ZONE = ZoneInfo("America/New_York")
HOUR = timedelta(hours=1)
PUBLISHED = NITEFLEX.newest_published_version()

# The 13 days between the account's connect date and the day the research was
# written, with the cycle day set to the 10th so the span is a whole cycle.
SAMPLE_START = datetime(2026, 8, 28, tzinfo=ZONE)
SAMPLE_END = datetime(2026, 9, 10, tzinfo=ZONE)
SAMPLE_SUPER_OFF_PEAK = 109.4
SAMPLE_OFF_PEAK = 207.3
SAMPLE_ON_PEAK = 267.4


def slug_at(hour: int) -> str:
    """Return the period NiteFlex's clock puts a local hour in."""
    if hour < 6:
        return SUPER_OFF_PEAK
    if 13 <= hour < 21:
        return ON_PEAK
    return OFF_PEAK


def cycle(start: datetime, end: datetime, *, partial: bool = False) -> Cycle:
    """Return a cycle between two local midnights."""
    return Cycle(start=start, end=end, partial=partial)


def spread(start: datetime, end: datetime, totals: dict[str, float]) -> list[HourUsage]:
    """Return every hour between two local moments, carrying period totals.

    Each period's kWh is divided evenly across the hours that belong to it, so
    the cycle's period sums are exactly the totals asked for.
    """
    moments = []
    moment = start.astimezone(UTC)
    while moment < end.astimezone(UTC):
        moments.append(moment)
        moment += HOUR
    counts: dict[str, int] = {}
    for moment in moments:
        slug = slug_at(moment.astimezone(ZONE).hour)
        counts[slug] = counts.get(slug, 0) + 1

    hours: list[HourUsage] = []
    for moment in moments:
        slug = slug_at(moment.astimezone(ZONE).hour)
        value = totals.get(slug, 0.0) / counts[slug]
        hours.append(HourUsage(start=moment, total=value, by_period={slug: value}))
    return hours


def one_hour(start: datetime, slug: str, value: float) -> HourUsage:
    """Return a single hour in one period."""
    return HourUsage(start=start, total=value, by_period={slug: value})


def totals(costs: Sequence[HourCost]) -> dict[str, float]:
    """Return each component summed over a priced cycle."""
    return {
        "energy": sum(cost.energy for cost in costs),
        "fixed": sum(cost.fixed for cost in costs),
        "rider": sum(cost.rider for cost in costs),
        "tax": sum(cost.tax for cost in costs),
        "adjustment": sum(cost.adjustment for cost in costs),
        "residual": sum(cost.residual for cost in costs),
        "total": sum(cost.total for cost in costs),
    }


def test_the_thirteen_day_sample_prices_as_the_research_computed_it() -> None:
    """584.1 kWh from the connect date comes to $52.99 of energy and $33 of service."""
    hours = spread(
        SAMPLE_START,
        SAMPLE_END,
        {
            SUPER_OFF_PEAK: SAMPLE_SUPER_OFF_PEAK,
            OFF_PEAK: SAMPLE_OFF_PEAK,
            ON_PEAK: SAMPLE_ON_PEAK,
        },
    )
    assert len(hours) == 312
    assert sum(hour.total for hour in hours) == pytest.approx(584.1)

    costs = NITEFLEX.price_cycle(
        cycle(SAMPLE_START, SAMPLE_END, partial=True), hours, [PUBLISHED], None
    )

    summed = totals(costs)
    assert len(costs) == 312
    assert summed["energy"] == pytest.approx(52.99, abs=0.05)
    assert summed["energy"] == pytest.approx(
        SAMPLE_OFF_PEAK * 0.075 + SAMPLE_ON_PEAK * 0.14
    )
    assert summed["fixed"] == pytest.approx(33.0)
    assert summed["rider"] == pytest.approx(0.0)
    assert summed["adjustment"] == pytest.approx(0.0)
    assert summed["residual"] == pytest.approx(0.0)
    assert summed["tax"] == pytest.approx(
        0.0775 * (summed["energy"] + summed["fixed"] + summed["rider"])
    )
    assert summed["total"] == pytest.approx(
        summed["energy"] + summed["fixed"] + summed["tax"]
    )


def test_the_allowance_runs_out_inside_an_hour() -> None:
    """The hour that crosses 400 kWh is part free and part billed."""
    start = datetime(2026, 9, 1, tzinfo=ZONE)
    hours = [
        one_hour(start.astimezone(UTC), SUPER_OFF_PEAK, 150.0),
        one_hour(start.astimezone(UTC) + HOUR, SUPER_OFF_PEAK, 150.0),
        one_hour(start.astimezone(UTC) + 2 * HOUR, SUPER_OFF_PEAK, 150.0),
        one_hour(start.astimezone(UTC) + 3 * HOUR, SUPER_OFF_PEAK, 50.0),
    ]

    costs = NITEFLEX.price_cycle(
        cycle(start, datetime(2026, 10, 1, tzinfo=ZONE)), hours, [PUBLISHED], None
    )

    assert [cost.energy for cost in costs] == [
        pytest.approx(0.0),
        pytest.approx(0.0),
        pytest.approx(50.0 * 0.05),
        pytest.approx(50.0 * 0.05),
    ]
    assert NITEFLEX.allowance_remaining(
        cycle(start, datetime(2026, 10, 1, tzinfo=ZONE)), hours, [PUBLISHED]
    ) == {SUPER_OFF_PEAK: 0.0}


def test_a_rider_skips_the_allowance_it_covered() -> None:
    """The PCA is charged on every kWh the allowance did not cover."""
    start = datetime(2026, 9, 1, tzinfo=ZONE)
    version = replace(PUBLISHED, pca_factor=0.01, super_off_peak_allowance=100.0)
    hours = [
        one_hour(start.astimezone(UTC), SUPER_OFF_PEAK, 150.0),
        one_hour(start.astimezone(UTC) + 13 * HOUR, ON_PEAK, 10.0),
    ]

    costs = NITEFLEX.price_cycle(
        cycle(start, datetime(2026, 10, 1, tzinfo=ZONE)), hours, [version], None
    )

    assert costs[0].rider == pytest.approx(0.01 * 50.0)
    assert costs[1].rider == pytest.approx(0.01 * 10.0)


def test_a_version_governs_from_local_midnight_on_its_day() -> None:
    """A mid-cycle rate change prices the days on each side under its own rates."""
    start = datetime(2026, 12, 15, tzinfo=ZONE)
    newer = replace(PUBLISHED, effective_from=date(2027, 1, 1), on_peak_rate=0.16)
    hours = [
        one_hour(datetime(2026, 12, 31, 13, tzinfo=ZONE).astimezone(UTC), ON_PEAK, 1.0),
        one_hour(datetime(2027, 1, 1, 13, tzinfo=ZONE).astimezone(UTC), ON_PEAK, 1.0),
    ]

    costs = NITEFLEX.price_cycle(
        cycle(start, datetime(2027, 1, 15, tzinfo=ZONE)),
        hours,
        [PUBLISHED, newer],
        None,
    )

    assert costs[0].energy == pytest.approx(0.14)
    assert costs[1].energy == pytest.approx(0.16)


def test_an_hour_before_every_version_is_a_failure() -> None:
    """Pricing without a rate is a configuration error, not a zero."""
    start = datetime(2025, 12, 1, tzinfo=ZONE)
    hours = [one_hour(start.astimezone(UTC), OFF_PEAK, 1.0)]

    with pytest.raises(ValueError, match="no rate version"):
        NITEFLEX.price_cycle(
            cycle(start, datetime(2026, 1, 1, tzinfo=ZONE)), hours, [PUBLISHED], None
        )


def test_the_fall_back_cycle_smears_over_an_extra_hour() -> None:
    """A 31-day span containing the fall-back holds 745 hours, not 744."""
    start = datetime(2026, 10, 15, tzinfo=ZONE)
    end = datetime(2026, 11, 15, tzinfo=ZONE)
    assert nominal_hours(cycle(start, end)) == 745

    costs = NITEFLEX.price_cycle(
        cycle(start, end),
        [one_hour(start.astimezone(UTC), SUPER_OFF_PEAK, 1.0)],
        [PUBLISHED],
        None,
    )

    assert costs[0].fixed == pytest.approx(33.0 / 745)


def test_an_unclassified_hour_costs_nothing_and_spends_no_allowance() -> None:
    """Usage the portal never classified has kWh and no dollars."""
    start = datetime(2026, 9, 1, tzinfo=ZONE)
    hours = [
        HourUsage(start=start.astimezone(UTC), total=500.0, by_period={}),
        one_hour(start.astimezone(UTC) + HOUR, SUPER_OFF_PEAK, 100.0),
    ]

    costs = NITEFLEX.price_cycle(
        cycle(start, datetime(2026, 10, 1, tzinfo=ZONE)), hours, [PUBLISHED], None
    )

    assert [cost.start for cost in costs] == [start.astimezone(UTC) + HOUR]
    assert costs[0].energy == pytest.approx(0.0)
    assert NITEFLEX.allowance_remaining(
        cycle(start, datetime(2026, 10, 1, tzinfo=ZONE)), hours, [PUBLISHED]
    ) == {SUPER_OFF_PEAK: 300.0}


def test_a_cycle_with_nothing_classified_prices_nothing() -> None:
    """A cycle the poll has not reached yet has no costs to write."""
    start = datetime(2026, 9, 1, tzinfo=ZONE)
    empty = cycle(start, datetime(2026, 10, 1, tzinfo=ZONE))

    assert NITEFLEX.price_cycle(empty, [], [PUBLISHED], None) == []
    assert NITEFLEX.allowance_remaining(empty, [], [PUBLISHED]) == {}


def test_a_cycle_with_no_version_reports_no_allowance() -> None:
    """Nothing is known about an allowance before the first rate version."""
    start = datetime(2025, 12, 1, tzinfo=ZONE)
    hours = [one_hour(start.astimezone(UTC), SUPER_OFF_PEAK, 1.0)]

    assert (
        NITEFLEX.allowance_remaining(
            cycle(start, datetime(2026, 1, 1, tzinfo=ZONE)), hours, [PUBLISHED]
        )
        == {}
    )


def test_a_finalized_cycle_adds_up_to_its_bill_over_the_hours_it_has() -> None:
    """The residual lands on the rows that exist, so a missing hour costs nothing.

    The fixed charges and the tax keep the nominal smear and leave the missing
    hour's share unbilled; the residual has to make up for that too, because
    the cycle's sum is what has to match the bill.
    """
    start = datetime(2026, 9, 1, tzinfo=ZONE)
    end = datetime(2026, 10, 1, tzinfo=ZONE)
    hours = spread(start, end, {SUPER_OFF_PEAK: 210.0, OFF_PEAK: 300.0, ON_PEAK: 290.0})
    del hours[100]
    assert len(hours) == 719
    actuals = CycleActuals(
        read_start=start.astimezone(UTC),
        read_end=end.astimezone(UTC),
        service_charge=31.90,
        pca_factor=0.004,
        tax=7.42,
        bill_total=112.00,
    )

    costs = NITEFLEX.price_cycle(cycle(start, end), hours, [PUBLISHED], actuals)

    summed = totals(costs)
    assert len(costs) == 719
    assert summed["total"] == pytest.approx(112.00, abs=1e-6)
    assert summed["fixed"] == pytest.approx(31.90 * 719 / 720)
    assert summed["tax"] == pytest.approx(7.42 * 719 / 720)
    assert summed["residual"] != pytest.approx(0.0)


def test_a_finalized_cycle_with_nothing_priced_cannot_carry_its_bill() -> None:
    """A bill total with no row to land on is a caller's mistake, not a zero."""
    start = datetime(2026, 9, 1, tzinfo=ZONE)
    end = datetime(2026, 10, 1, tzinfo=ZONE)
    actuals = CycleActuals(
        read_start=start.astimezone(UTC),
        read_end=end.astimezone(UTC),
        service_charge=31.90,
        pca_factor=0.004,
        tax=7.42,
        bill_total=112.00,
    )

    with pytest.raises(ValueError, match="no priced hours"):
        NITEFLEX.price_cycle(cycle(start, end), [], [PUBLISHED], actuals)


def test_a_naive_cycle_is_refused() -> None:
    """Local midnight without a zone is not a cycle bound."""
    naive = Cycle(start=datetime(2026, 9, 1), end=datetime(2026, 10, 1), partial=False)

    with pytest.raises(ValueError, match="timezone aware"):
        NITEFLEX.price_cycle(naive, [], [PUBLISHED], None)


def test_the_rate_schedule_codes_pick_the_tariff() -> None:
    """SmartHub qualifies its codes with the co-op that issued them."""
    assert tariff_for_codes(["NFON:COBB", "NFOFF:COBB", "NFSOF:COBB"]) is NITEFLEX
    assert tariff_for_codes(["RES:COBB"]) is None


def test_a_rate_version_survives_a_trip_through_entry_data() -> None:
    """Entry data holds JSON, so the effective date travels as a string."""
    stored = rate_version_to_data(PUBLISHED)

    assert stored["effective_from"] == "2026-01-01"
    assert rate_version_from_data(stored) == PUBLISHED


def test_the_period_labels_match_the_portal_case_insensitively() -> None:
    """The portal's labels are what the tariff maps from."""
    assert NITEFLEX.slug_for_label("super off pk") == SUPER_OFF_PEAK
    assert NITEFLEX.slug_for_label("Peak Demand") is None


def test_the_newest_published_version_is_the_seed() -> None:
    """Setup seeds the entry from the newest version the package ships."""
    older = RateVersion(
        effective_from=date(2025, 1, 1),
        on_peak_rate=0.13,
        off_peak_rate=0.07,
        super_off_peak_rate=0.05,
        super_off_peak_allowance=400.0,
        service_charge=30.0,
        pca_factor=0.0,
        sales_tax_rate=0.0775,
        recurring_adjustment=0.0,
    )
    tariff = replace(NITEFLEX, published_versions=(older, PUBLISHED))

    assert tariff.newest_published_version() is PUBLISHED


def test_a_recurring_adjustment_is_smeared_and_untaxed() -> None:
    """A standing credit lands outside the tax base, by the hour."""
    start = datetime(2026, 9, 1, tzinfo=ZONE)
    end = datetime(2026, 10, 1, tzinfo=ZONE)
    version = replace(PUBLISHED, recurring_adjustment=-15.0)
    hours = [one_hour(start.astimezone(UTC), ON_PEAK, 2.0)]

    costs = NITEFLEX.price_cycle(cycle(start, end), hours, [version], None)

    hourly = nominal_hours(cycle(start, end))
    assert costs[0].adjustment == pytest.approx(-15.0 / hourly)
    assert costs[0].tax == pytest.approx(
        0.0775 * (costs[0].energy + costs[0].fixed + costs[0].rider)
    )
