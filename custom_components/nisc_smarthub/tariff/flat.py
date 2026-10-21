"""Flat-rate schedules, and New Hampshire Electric Co-op's residential one.

NHEC's numbers are its Schedule of Rates effective 2026-02-01 and 2026-08-01
(https://www.nhec.com/schedule-of-fees-rates/). The board sets the Co-op Power
and Regional Access charges on a bills-rendered basis, and a member's bills
showed whole cycles priced at one version and a short first bill prorating the
member charge by days over 30 (GitHub issue #37).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, tzinfo

from .base import (
    Cycle,
    CycleActuals,
    HourCost,
    HourUsage,
    Parameter,
    PricedCycle,
    RateVersion,
    Tariff,
    TariffSource,
    nominal_hours,
    version_for,
)

SERVICE_CHARGE = "service_charge"
PRORATION_DAYS = 30


@dataclass(frozen=True, kw_only=True)
class FlatTariff(Tariff):
    """Every kWh at the sum of the per-kWh lines, plus one monthly charge.

    A whole cycle is priced under the version in effect on the day its bill is
    rendered, which is taken to be the day the cycle ends until a bill supplies
    its read end. A first cycle the connect date cuts short pays the monthly
    charge for its days over 30; every other cycle pays it whole.
    """

    energy_rates: tuple[str, ...]

    def __post_init__(self) -> None:
        """Refuse per-kWh lines the rate parameters do not declare."""
        super().__post_init__()
        declared = {parameter.key for parameter in self.rate_parameters}
        if not {*self.energy_rates, SERVICE_CHARGE} <= declared:
            raise ValueError(f"{self.key}: undeclared flat-rate parameters")

    def governs_cycle(self, version: RateVersion, cycle: Cycle) -> bool:
        """Say whether the version is in effect on the day the cycle's bill is."""
        return version.effective_from <= _bill_day(cycle, None)

    def price_cycle(
        self,
        cycle: Cycle,
        hours: Sequence[HourUsage],
        versions: Sequence[RateVersion],
        actuals: CycleActuals | None,
    ) -> PricedCycle:
        """Price every hour of the cycle under the version its bill is rendered at.

        The monthly charge is smeared over the cycle's nominal hours, so an hour
        the portal has not reported yet leaves its share unbilled.
        """
        priced = sorted(hours, key=lambda hour: hour.start)
        if not priced:
            return PricedCycle(costs=[], allowance_remaining={})
        bill_day = _bill_day(cycle, actuals)
        version = version_for(versions, bill_day)
        if version is None:
            raise ValueError(f"no rate version is in effect on {bill_day.isoformat()}")

        per_kwh = sum(version.values[rate] for rate in self.energy_rates)
        if actuals is not None:
            monthly = actuals.values[SERVICE_CHARGE]
        elif cycle.partial:
            monthly = version.values[SERVICE_CHARGE] * _days(cycle) / PRORATION_DAYS
        else:
            monthly = version.values[SERVICE_CHARGE]
        fixed = monthly / nominal_hours(cycle)
        return PricedCycle(
            costs=[
                HourCost(
                    start=hour.start,
                    energy=hour.total * per_kwh,
                    fixed=fixed,
                    rider=0.0,
                    tax=0.0,
                    adjustment=0.0,
                    residual=0.0,
                )
                for hour in priced
            ],
            allowance_remaining={},
        )


def _bill_day(cycle: Cycle, actuals: CycleActuals | None) -> date:
    end = cycle.end if actuals is None else actuals.read_end
    return end.astimezone(_zone(cycle)).date()


def _days(cycle: Cycle) -> int:
    zone = _zone(cycle)
    return (
        cycle.end.astimezone(zone).date() - cycle.start.astimezone(zone).date()
    ).days


def _zone(cycle: Cycle) -> tzinfo:
    if cycle.start.tzinfo is None:
        raise ValueError("a cycle's bounds must be timezone aware")
    return cycle.start.tzinfo


DELIVERY_RATE = "delivery_rate"
SYSTEM_BENEFITS_RATE = "system_benefits_rate"
REGIONAL_ACCESS_RATE = "regional_access_rate"
CO_OP_POWER_RATE = "co_op_power_rate"

NHEC_RESIDENTIAL = FlatTariff(
    key="nhec_residential",
    display_name="NHEC Residential (B)",
    source=TariffSource.USAGE,
    periods=(),
    allowances=(),
    portal_hosts=("nhec.smarthub.coop",),
    rate_schedule_codes=("B:COOP",),
    energy_rates=(
        DELIVERY_RATE,
        SYSTEM_BENEFITS_RATE,
        REGIONAL_ACCESS_RATE,
        CO_OP_POWER_RATE,
    ),
    rate_parameters=(
        Parameter(key=SERVICE_CHARGE, signed=False),
        Parameter(key=DELIVERY_RATE, signed=False),
        Parameter(key=SYSTEM_BENEFITS_RATE, signed=False),
        Parameter(key=REGIONAL_ACCESS_RATE, signed=False),
        Parameter(key=CO_OP_POWER_RATE, signed=False),
    ),
    bill_parameters=(Parameter(key=SERVICE_CHARGE, signed=False),),
    published_versions=(
        RateVersion(
            effective_from=date(2026, 2, 1),
            values={
                SERVICE_CHARGE: 29.0,
                DELIVERY_RATE: 0.05868,
                SYSTEM_BENEFITS_RATE: 0.00778,
                REGIONAL_ACCESS_RATE: 0.04290,
                CO_OP_POWER_RATE: 0.11117,
            },
        ),
        RateVersion(
            effective_from=date(2026, 8, 1),
            values={
                SERVICE_CHARGE: 29.0,
                DELIVERY_RATE: 0.05868,
                SYSTEM_BENEFITS_RATE: 0.00778,
                REGIONAL_ACCESS_RATE: 0.04099,
                CO_OP_POWER_RATE: 0.13968,
            },
        ),
    ),
)
