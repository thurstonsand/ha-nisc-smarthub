"""Cobb EMC's NiteFlex residential schedule.

The published numbers, the allowance's exclusion from the Power Cost Adjustment,
and the 7.75% Fulton County rate are transcribed from
`docs/wayfinding/nisc-smarthub-integration/research/niteflex-tariff.md` and
`research/bill-anatomy.md`, which cite the schedule PDFs and the Georgia
Department of Revenue rate chart.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, tzinfo

from custom_components.nisc_smarthub.smarthub import Charge

from .base import (
    POWER_COST_ADJUSTMENT_LINE,
    SERVICE_LINE,
    TAX_LINE,
    USAGE_LINE,
    Cycle,
    CycleActuals,
    HourCost,
    HourUsage,
    Parameter,
    Period,
    PricedCycle,
    RateVersion,
    Tariff,
    TariffSource,
    nominal_hours,
    total_of,
    version_for,
)

ON_PEAK = "on_peak"
OFF_PEAK = "off_peak"
SUPER_OFF_PEAK = "super_off_peak"

ON_PEAK_RATE = "on_peak_rate"
OFF_PEAK_RATE = "off_peak_rate"
SUPER_OFF_PEAK_RATE = "super_off_peak_rate"
SUPER_OFF_PEAK_ALLOWANCE = "super_off_peak_allowance"
SERVICE_CHARGE = "service_charge"
PCA_FACTOR = "pca_factor"
SALES_TAX_RATE = "sales_tax_rate"
RECURRING_ADJUSTMENT = "recurring_adjustment"
TAX = "tax"


@dataclass(frozen=True, kw_only=True)
class NiteFlexTariff(Tariff):
    """Three periods, one allowance, one service charge, one rider."""

    def governs_cycle(self, version: RateVersion, cycle: Cycle) -> bool:
        """Say whether the version is in effect from the cycle's first local day."""
        return version.effective_from <= cycle.start.astimezone(_zone(cycle)).date()

    def price_cycle(
        self,
        cycle: Cycle,
        hours: Sequence[HourUsage],
        versions: Sequence[RateVersion],
        actuals: CycleActuals | None,
    ) -> PricedCycle:
        """Price one whole cycle, oldest hour first.

        The Super Off-Peak allowance is consumed chronologically across the
        cycle and sized by the version governing the cycle's first priced hour,
        because it is a per-cycle quantity and a mid-cycle rate change does not
        restart it. Rates follow the version governing each hour's own local
        day, from local midnight on its `effective_from`. Fixed charges are
        smeared over the cycle's nominal hours, so an hour the portal has not
        reported yet leaves its share unbilled.
        """
        zone = _zone(cycle)
        priced = _priced(hours)
        if not priced:
            return PricedCycle(costs=[], allowance_remaining={})

        opening = version_for(versions, priced[0].start.astimezone(zone).date())
        if opening is None:
            raise ValueError(
                f"no rate version is in effect on "
                f"{priced[0].start.astimezone(zone).date().isoformat()}"
            )
        hourly = nominal_hours(cycle)
        bill = None if actuals is None else actuals.values

        allowance = opening.values[SUPER_OFF_PEAK_ALLOWANCE]
        costs: list[HourCost] = []
        for hour in priced:
            # Every hour after the opening one is at or after it, so a version
            # governs it whenever one governed the opening hour.
            version = version_for(versions, hour.start.astimezone(zone).date())
            assert version is not None
            free = min(allowance, hour.by_period.get(SUPER_OFF_PEAK, 0.0))
            allowance -= free
            costs.append(_components(hour, version.values, free, hourly, bill))
        used = sum(hour.by_period.get(SUPER_OFF_PEAK, 0.0) for hour in priced)
        return PricedCycle(
            costs=costs,
            allowance_remaining={
                SUPER_OFF_PEAK: max(
                    opening.values[SUPER_OFF_PEAK_ALLOWANCE] - used, 0.0
                )
            },
        )

    def read_bill(self, charges: Sequence[Charge]) -> dict[str, float]:
        """Read the service charge, the tax, and the PCA factor off a bill.

        The PCA applies to the kWh the schedule charged for, which the per-kWh
        lines carry, so the free allowance stays out of the factor's divisor.
        """
        charged = sum(
            charge.usage for charge in charges if charge.charge_type == USAGE_LINE
        )
        pca = total_of(charges, POWER_COST_ADJUSTMENT_LINE)
        return {
            SERVICE_CHARGE: total_of(charges, SERVICE_LINE),
            PCA_FACTOR: pca / charged if charged else 0.0,
            TAX: total_of(charges, TAX_LINE),
        }


def _components(
    hour: HourUsage,
    rates: Mapping[str, float],
    free: float,
    hourly: int,
    bill: Mapping[str, float] | None,
) -> HourCost:
    service_charge = rates[SERVICE_CHARGE] if bill is None else bill[SERVICE_CHARGE]
    pca_factor = rates[PCA_FACTOR] if bill is None else bill[PCA_FACTOR]

    energy = (
        hour.by_period.get(ON_PEAK, 0.0) * rates[ON_PEAK_RATE]
        + hour.by_period.get(OFF_PEAK, 0.0) * rates[OFF_PEAK_RATE]
        + (hour.by_period.get(SUPER_OFF_PEAK, 0.0) - free) * rates[SUPER_OFF_PEAK_RATE]
    )
    fixed = service_charge / hourly
    rider = pca_factor * (hour.total - free)
    tax = (
        rates[SALES_TAX_RATE] * (energy + fixed + rider)
        if bill is None
        else bill[TAX] / hourly
    )
    return HourCost(
        start=hour.start,
        energy=energy,
        fixed=fixed,
        rider=rider,
        tax=tax,
        adjustment=rates[RECURRING_ADJUSTMENT] / hourly,
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
    allowances=(SUPER_OFF_PEAK,),
    portal_hosts=("cobbemc.smarthub.coop",),
    rate_schedule_codes=("NFON:COBB", "NFOFF:COBB", "NFSOF:COBB"),
    rate_parameters=(
        Parameter(key=ON_PEAK_RATE, signed=False),
        Parameter(key=OFF_PEAK_RATE, signed=False),
        Parameter(key=SUPER_OFF_PEAK_RATE, signed=False),
        Parameter(key=SUPER_OFF_PEAK_ALLOWANCE, signed=False),
        Parameter(key=SERVICE_CHARGE, signed=False),
        Parameter(key=PCA_FACTOR, signed=True),
        Parameter(key=SALES_TAX_RATE, signed=False),
        Parameter(key=RECURRING_ADJUSTMENT, signed=True),
    ),
    bill_parameters=(
        Parameter(key=SERVICE_CHARGE, signed=False),
        Parameter(key=PCA_FACTOR, signed=True),
        Parameter(key=TAX, signed=False),
    ),
    published_versions=(
        RateVersion(
            effective_from=date(2026, 1, 1),
            values={
                ON_PEAK_RATE: 0.14,
                OFF_PEAK_RATE: 0.075,
                SUPER_OFF_PEAK_RATE: 0.05,
                SUPER_OFF_PEAK_ALLOWANCE: 400.0,
                SERVICE_CHARGE: 33.0,
                PCA_FACTOR: 0.0,
                SALES_TAX_RATE: 0.0775,
                RECURRING_ADJUSTMENT: 0.0,
            },
        ),
    ),
)
