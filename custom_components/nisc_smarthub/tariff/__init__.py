"""The tariffs this integration can price with."""

from collections.abc import Iterable

from .base import (
    EFFECTIVE_FROM,
    CostComponents,
    Cycle,
    CycleActuals,
    HourCost,
    HourUsage,
    InvalidValue,
    Parameter,
    Period,
    PricedCycle,
    RateVersion,
    Tariff,
    TariffSource,
    conform_values,
    nominal_hours,
    rate_version_to_data,
    settle,
    sum_components,
    version_for,
)
from .niteflex import NITEFLEX

TARIFFS: dict[str, Tariff] = {NITEFLEX.key: NITEFLEX}


def tariff_for(host: str, codes: Iterable[str]) -> Tariff | None:
    """Return the tariff a portal and a location's active rate schedules name.

    SmartHub qualifies each code with the provider that issued it (`NFON:COBB`),
    and a qualifier is not unique across co-ops, so the portal has to match too.
    """
    seen = {code.upper() for code in codes}
    matches = [
        tariff
        for tariff in TARIFFS.values()
        if host.lower() in tariff.portal_hosts
        and seen.issuperset(code.upper() for code in tariff.rate_schedule_codes)
    ]
    if len(matches) > 1:
        raise ValueError(
            f"tariffs {', '.join(one.key for one in matches)} all match {host}"
        )
    return matches[0] if matches else None


__all__ = [
    "EFFECTIVE_FROM",
    "NITEFLEX",
    "TARIFFS",
    "CostComponents",
    "Cycle",
    "CycleActuals",
    "HourCost",
    "HourUsage",
    "InvalidValue",
    "Parameter",
    "Period",
    "PricedCycle",
    "RateVersion",
    "Tariff",
    "TariffSource",
    "conform_values",
    "nominal_hours",
    "rate_version_to_data",
    "settle",
    "sum_components",
    "tariff_for",
    "version_for",
]
