"""The tariffs this integration can price with."""

from collections.abc import Iterable

from .base import (
    CostComponents,
    Cycle,
    CycleActuals,
    HourCost,
    HourUsage,
    InvalidRateVersion,
    Period,
    RateVersion,
    Tariff,
    TariffSource,
    nominal_hours,
    rate_version_from_data,
    rate_version_to_data,
    sum_components,
    validate_rate_version,
    version_for,
)
from .niteflex import NITEFLEX

TARIFFS: dict[str, Tariff] = {NITEFLEX.key: NITEFLEX}

CODE_QUALIFIER = ":"


def tariff_for_codes(codes: Iterable[str]) -> Tariff | None:
    """Return the tariff a location's active rate schedule codes name.

    SmartHub qualifies each code with the co-op that issued it (`NFON:COBB`),
    so the qualifier is dropped before matching.
    """
    seen = {code.split(CODE_QUALIFIER)[0].upper() for code in codes}
    for tariff in TARIFFS.values():
        if seen.issuperset(tariff.rate_schedule_codes):
            return tariff
    return None


__all__ = [
    "NITEFLEX",
    "TARIFFS",
    "CostComponents",
    "Cycle",
    "CycleActuals",
    "HourCost",
    "HourUsage",
    "InvalidRateVersion",
    "Period",
    "RateVersion",
    "Tariff",
    "TariffSource",
    "nominal_hours",
    "rate_version_from_data",
    "rate_version_to_data",
    "sum_components",
    "tariff_for_codes",
    "validate_rate_version",
    "version_for",
]
