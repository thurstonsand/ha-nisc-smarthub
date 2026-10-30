"""NHEC's flat residential schedule, checked against a member's bills.

The bills are the ones transcribed in GitHub issue #37: 621 kWh billed $182.46
on the August 2026 rates, a seven-day first bill charging $6.77 of the $29
member charge, and the cycles either side of the August 1 change each billed
whole at one version.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.nisc_smarthub.smarthub import Charge
from custom_components.nisc_smarthub.tariff import (
    NHEC_RESIDENTIAL,
    Cycle,
    CycleActuals,
    HourUsage,
    tariff_for,
)

ZONE = ZoneInfo("America/New_York")
HOUR = timedelta(hours=1)
VERSIONS = NHEC_RESIDENTIAL.published_versions


def evenly(start: datetime, end: datetime, kwh: float) -> list[HourUsage]:
    """Return every hour between two local moments, sharing `kwh` evenly."""
    moments: list[datetime] = []
    moment = start.astimezone(UTC)
    while moment < end.astimezone(UTC):
        moments.append(moment)
        moment += HOUR
    return [
        HourUsage(start=one, total=kwh / len(moments), by_period={}) for one in moments
    ]


def priced_total(start: datetime, end: datetime, kwh: float, *, partial: bool) -> float:
    """Return what a cycle of evenly spread usage costs."""
    cycle = Cycle(start=start, end=end, partial=partial)
    priced = NHEC_RESIDENTIAL.price_cycle(
        cycle, evenly(start, end, kwh), VERSIONS, None
    )
    return sum(cost.total for cost in priced.costs)


def test_a_whole_cycle_comes_to_its_bill() -> None:
    """621 kWh at 24.713 cents plus the member charge; billed $182.46."""
    start = datetime(2026, 8, 7, tzinfo=ZONE)
    end = datetime(2026, 9, 8, tzinfo=ZONE)

    total = priced_total(start, end, 621.0, partial=False)

    assert total == pytest.approx(182.46, abs=0.01)


@pytest.mark.parametrize(
    ("start", "end", "per_kwh"),
    [
        pytest.param(date(2026, 6, 5), date(2026, 7, 7), 0.22053, id="before_august"),
        pytest.param(
            date(2026, 7, 7), date(2026, 8, 7), 0.24713, id="straddling_august"
        ),
    ],
)
def test_a_cycle_is_billed_whole_at_the_version_of_its_bill(
    start: date, end: date, per_kwh: float
) -> None:
    """July's hours in the cycle billed August 7 cost August's rates."""
    first = datetime(start.year, start.month, start.day, tzinfo=ZONE)
    last = datetime(end.year, end.month, end.day, tzinfo=ZONE)

    total = priced_total(first, last, 500.0, partial=False)

    assert total == pytest.approx(500.0 * per_kwh + 29.0)


def test_a_short_first_cycle_pays_the_member_charge_by_its_days() -> None:
    """A seven-day first bill charged $6.77 of the $29."""
    start = datetime(2026, 3, 21, tzinfo=ZONE)
    end = datetime(2026, 3, 28, tzinfo=ZONE)

    cycle = Cycle(start=start, end=end, partial=True)
    priced = NHEC_RESIDENTIAL.price_cycle(
        cycle, evenly(start, end, 0.0), VERSIONS, None
    )

    assert sum(cost.fixed for cost in priced.costs) == pytest.approx(6.77, abs=0.005)


def test_a_finalized_cycle_takes_its_member_charge_and_version_from_the_bill() -> None:
    """The read end, not the calendar end, decides which version prices it."""
    start = datetime(2026, 7, 1, tzinfo=ZONE)
    end = datetime(2026, 8, 1, tzinfo=ZONE)
    actuals = CycleActuals(
        read_start=start,
        read_end=datetime(2026, 7, 30, tzinfo=ZONE),
        bill_total=150.0,
        values={"service_charge": 25.0},
    )

    priced = NHEC_RESIDENTIAL.price_cycle(
        Cycle(start=start, end=end, partial=False),
        evenly(start, end, 600.0),
        VERSIONS,
        actuals,
    )

    assert sum(cost.energy for cost in priced.costs) == pytest.approx(600.0 * 0.22053)
    assert sum(cost.fixed for cost in priced.costs) == pytest.approx(25.0)


def test_a_cycle_billed_before_every_version_is_a_failure() -> None:
    """Pricing without a rate is a configuration error, not a zero."""
    start = datetime(2025, 12, 1, tzinfo=ZONE)
    end = datetime(2026, 1, 1, tzinfo=ZONE)

    with pytest.raises(ValueError, match="no rate version"):
        priced_total(start, end, 1.0, partial=False)


def test_a_version_governs_a_cycle_whose_bill_it_is_in_effect_for() -> None:
    """February's rates cannot price a cycle billed in January."""
    february, _ = VERSIONS
    january = Cycle(
        start=datetime(2025, 12, 28, tzinfo=ZONE),
        end=datetime(2026, 1, 28, tzinfo=ZONE),
        partial=False,
    )
    march = Cycle(
        start=datetime(2026, 1, 28, tzinfo=ZONE),
        end=datetime(2026, 2, 28, tzinfo=ZONE),
        partial=False,
    )

    assert NHEC_RESIDENTIAL.governs_cycle(february, january) is False
    assert NHEC_RESIDENTIAL.governs_cycle(february, march) is True


@pytest.mark.parametrize(
    ("host", "codes", "matched"),
    [
        pytest.param("nhec.smarthub.coop", ["B:COOP"], True, id="nhec"),
        pytest.param("nhec.smarthub.coop", ["B:COOP", "EVF:COOP"], True, id="and_more"),
        pytest.param("nhec.smarthub.coop", ["B2:COOP"], False, id="general_service"),
        pytest.param("other.smarthub.coop", ["B:COOP"], False, id="another_co_op"),
    ],
)
def test_schedule_b_at_nhec_picks_the_tariff(
    host: str, codes: list[str], matched: bool
) -> None:
    """`COOP` is a generic qualifier, so only NHEC's portal makes it NHEC's."""
    assert (tariff_for(host, codes) is NHEC_RESIDENTIAL) is matched


def test_a_bill_supplies_the_member_charge() -> None:
    """Every service line counts toward it, and nothing else does."""
    charges = [
        Charge(
            charge_type=charge_type,
            service_location="70001",
            industry="ELECTRIC",
            rate_schedule="B",
            usage=usage,
            amount=amount,
        )
        for charge_type, usage, amount in (
            ("USAGE", 621.0, 153.46),
            ("SERVICE", 0.0, 29.0),
            ("OTHER", 0.0, 4.0),
        )
    ]

    assert NHEC_RESIDENTIAL.read_bill(charges) == {"service_charge": 29.0}
