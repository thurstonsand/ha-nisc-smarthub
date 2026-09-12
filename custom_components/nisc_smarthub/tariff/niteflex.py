"""Cobb EMC's NiteFlex residential schedule.

The published numbers, the allowance's exclusion from the Power Cost Adjustment,
and the 7.75% Fulton County rate are transcribed from
`docs/wayfinding/nisc-smarthub-integration/research/niteflex-tariff.md` and
`research/bill-anatomy.md`, which cite the schedule PDFs and the Georgia
Department of Revenue rate chart.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, tzinfo

from .base import (
    Cycle,
    CycleActuals,
    HourCost,
    HourUsage,
    Period,
    RateVersion,
    Tariff,
    TariffSource,
    nominal_hours,
    version_for,
)

ON_PEAK = "on_peak"
OFF_PEAK = "off_peak"
SUPER_OFF_PEAK = "super_off_peak"


@dataclass(frozen=True, kw_only=True)
class NiteFlexTariff(Tariff):
    """Three periods, one allowance, one service charge, one rider."""

    def price_cycle(
        self,
        cycle: Cycle,
        hours: Sequence[HourUsage],
        versions: Sequence[RateVersion],
        actuals: CycleActuals | None,
    ) -> list[HourCost]:
        """Price one whole cycle, oldest hour first.

        The Super Off-Peak allowance is consumed chronologically across the
        cycle and sized by the version governing the cycle's first priced hour,
        because it is a per-cycle quantity and a mid-cycle rate change does not
        restart it. Rates follow the version governing each hour's own local
        day. Fixed charges are smeared over the cycle's nominal hours, so an
        hour the portal has not reported yet leaves its share unbilled. The
        residual of a finalized cycle is smeared over the priced hours instead,
        because those are the rows that will exist and the bill has to add up
        across them.
        """
        zone = _zone(cycle)
        priced = _priced(hours)
        if not priced:
            if actuals is not None:
                raise ValueError(
                    "a finalized cycle with no priced hours cannot carry its bill"
                )
            return []

        opening = version_for(versions, priced[0].start.astimezone(zone).date())
        if opening is None:
            raise ValueError(
                f"no rate version is in effect on "
                f"{priced[0].start.astimezone(zone).date().isoformat()}"
            )
        hourly = nominal_hours(cycle)

        allowance = opening.super_off_peak_allowance
        costs: list[HourCost] = []
        for hour in priced:
            # Every hour after the opening one is at or after it, so a version
            # governs it whenever one governed the opening hour.
            version = version_for(versions, hour.start.astimezone(zone).date())
            assert version is not None
            free = min(allowance, hour.by_period.get(SUPER_OFF_PEAK, 0.0))
            allowance -= free
            costs.append(_components(hour, version, free, hourly, actuals))

        if actuals is None:
            return costs
        residual = (actuals.bill_total - sum(cost.total for cost in costs)) / len(costs)
        return [replace(cost, residual=residual) for cost in costs]

    def allowance_remaining(
        self,
        cycle: Cycle,
        hours: Sequence[HourUsage],
        versions: Sequence[RateVersion],
    ) -> dict[str, float]:
        """Return the Super Off-Peak kWh still free in this cycle."""
        priced = _priced(hours)
        if not priced:
            return {}
        opening = version_for(versions, priced[0].start.astimezone(_zone(cycle)).date())
        if opening is None:
            return {}
        used = sum(hour.by_period.get(SUPER_OFF_PEAK, 0.0) for hour in priced)
        return {SUPER_OFF_PEAK: max(opening.super_off_peak_allowance - used, 0.0)}


def _components(
    hour: HourUsage,
    version: RateVersion,
    free: float,
    hourly: int,
    actuals: CycleActuals | None,
) -> HourCost:
    service_charge = (
        version.service_charge if actuals is None else actuals.service_charge
    )
    pca_factor = version.pca_factor if actuals is None else actuals.pca_factor

    energy = (
        hour.by_period.get(ON_PEAK, 0.0) * version.on_peak_rate
        + hour.by_period.get(OFF_PEAK, 0.0) * version.off_peak_rate
        + (hour.by_period.get(SUPER_OFF_PEAK, 0.0) - free) * version.super_off_peak_rate
    )
    fixed = service_charge / hourly
    rider = pca_factor * (hour.total - free)
    tax = (
        version.sales_tax_rate * (energy + fixed + rider)
        if actuals is None
        else actuals.tax / hourly
    )
    return HourCost(
        start=hour.start,
        energy=energy,
        fixed=fixed,
        rider=rider,
        tax=tax,
        adjustment=version.recurring_adjustment / hourly,
        residual=0.0,
    )


def _priced(hours: Sequence[HourUsage]) -> list[HourUsage]:
    return sorted(
        (hour for hour in hours if hour.by_period), key=lambda hour: hour.start
    )


def _zone(cycle: Cycle) -> tzinfo:
    if cycle.start.tzinfo is None:
        raise ValueError("a cycle's bounds must be timezone aware")
    return cycle.start.tzinfo


NITEFLEX = NiteFlexTariff(
    key="niteflex",
    display_name="Cobb EMC NiteFlex",
    source=TariffSource.TIME_OF_USE,
    periods=(
        Period(label="On Peak", slug=ON_PEAK),
        Period(label="Off Peak", slug=OFF_PEAK),
        Period(label="Super Off Pk", slug=SUPER_OFF_PEAK),
    ),
    tiered_periods=(SUPER_OFF_PEAK,),
    rate_schedule_codes=("NFON", "NFOFF", "NFSOF"),
    published_versions=(
        RateVersion(
            effective_from=date(2026, 1, 1),
            on_peak_rate=0.14,
            off_peak_rate=0.075,
            super_off_peak_rate=0.05,
            super_off_peak_allowance=400.0,
            service_charge=33.0,
            pca_factor=0.0,
            sales_tax_rate=0.0775,
            recurring_adjustment=0.0,
        ),
    ),
)
