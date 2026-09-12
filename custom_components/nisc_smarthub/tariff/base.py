"""What every tariff is, and what pricing one cycle takes and returns.

A tariff is stateless: it is handed a whole cycle, the hours that arrived in it,
the rate versions that govern them, and the bill's actuals once there is a bill.
Tiering, the service-charge smear, and proration live behind `price_cycle`, so
nothing downstream carries a tariff's vocabulary.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
import math
from typing import Any

HOUR_SECONDS = 3600


@dataclass(frozen=True, kw_only=True)
class Cycle:
    """One billing cycle, half-open, in the portal's local zone."""

    start: datetime
    end: datetime
    partial: bool


class TariffSource(StrEnum):
    """Which poll series a tariff prices from."""

    TIME_OF_USE = "TIME_OF_USE"
    USAGE = "USAGE"


@dataclass(frozen=True, kw_only=True)
class Period:
    """One time-of-use bucket: the portal's label and this integration's slug."""

    label: str
    slug: str


@dataclass(frozen=True, kw_only=True)
class RateVersion:
    """One dated set of a tariff's numbers.

    The version governs every hour from local midnight on `effective_from`,
    mid-cycle included, which is how the schedule states its own effective date.
    """

    effective_from: date
    on_peak_rate: float
    off_peak_rate: float
    super_off_peak_rate: float
    super_off_peak_allowance: float
    service_charge: float
    pca_factor: float
    sales_tax_rate: float
    recurring_adjustment: float


# A rate, an allowance, a charge, or a tax rate is a quantity; the two riders
# are signed corrections. The split decides which fields may go negative.
NON_NEGATIVE_FIELDS = (
    "on_peak_rate",
    "off_peak_rate",
    "super_off_peak_rate",
    "super_off_peak_allowance",
    "service_charge",
    "sales_tax_rate",
)
SIGNED_FIELDS = ("pca_factor", "recurring_adjustment")


class InvalidRateVersion(ValueError):
    """A rate version the user typed does not describe a price.

    `field` is the offending input and `reason` a translation key the form
    shows against it.
    """

    def __init__(self, field: str, reason: str) -> None:
        """Name the field and why it was refused."""
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


def validate_rate_version(data: dict[str, Any]) -> RateVersion:
    """Conform one form's rate fields into a version, or refuse them.

    Every number has to be finite; the quantities also have to be zero or
    more. The effective date is left to the caller, who knows what it has to
    be compared against.
    """
    for field in NON_NEGATIVE_FIELDS:
        value = _finite(data, field)
        if value < 0:
            raise InvalidRateVersion(field, "negative_number")
    for field in SIGNED_FIELDS:
        _finite(data, field)
    return rate_version_from_data(data)


def _finite(data: dict[str, Any], field: str) -> float:
    try:
        value = float(data[field])
    except (KeyError, TypeError, ValueError) as err:
        raise InvalidRateVersion(field, "not_a_number") from err
    if not math.isfinite(value):
        raise InvalidRateVersion(field, "not_a_number")
    return value


@dataclass(frozen=True, kw_only=True)
class HourUsage:
    """One hour of metered usage, split the way the utility classified it.

    `by_period` is empty when the hour arrived in the usage series but landed
    in no period this tariff knows. Such an hour is worth kWh and no dollars.
    """

    start: datetime
    total: float
    by_period: dict[str, float]


@dataclass(frozen=True, kw_only=True)
class CycleActuals:
    """The values a posted bill supplies for one cycle."""

    read_start: datetime
    read_end: datetime
    service_charge: float
    pca_factor: float
    tax: float
    bill_total: float


@dataclass(frozen=True, kw_only=True)
class CostComponents:
    """A cost split the way the bill itemizes it."""

    energy: float
    fixed: float
    rider: float
    tax: float
    adjustment: float
    residual: float

    @property
    def total(self) -> float:
        """Return every component added up, which is what the bill charges."""
        return (
            self.energy
            + self.fixed
            + self.rider
            + self.tax
            + self.adjustment
            + self.residual
        )


def sum_components(costs: Sequence[CostComponents]) -> CostComponents:
    """Return the component-wise sum of many costs."""
    return CostComponents(
        energy=sum(cost.energy for cost in costs),
        fixed=sum(cost.fixed for cost in costs),
        rider=sum(cost.rider for cost in costs),
        tax=sum(cost.tax for cost in costs),
        adjustment=sum(cost.adjustment for cost in costs),
        residual=sum(cost.residual for cost in costs),
    )


@dataclass(frozen=True, kw_only=True)
class HourCost(CostComponents):
    """What one hour costs.

    `start` rides along so an unpriced hour is simply absent from the result
    rather than a hole a caller has to line up by index.
    """

    start: datetime


@dataclass(frozen=True, kw_only=True)
class Tariff(ABC):
    """One rate schedule, executable."""

    key: str
    display_name: str
    source: TariffSource
    periods: tuple[Period, ...]
    tiered_periods: tuple[str, ...]
    rate_schedule_codes: tuple[str, ...]
    published_versions: tuple[RateVersion, ...]

    @abstractmethod
    def price_cycle(
        self,
        cycle: Cycle,
        hours: Sequence[HourUsage],
        versions: Sequence[RateVersion],
        actuals: CycleActuals | None,
    ) -> list[HourCost]:
        """Return one cost per priced hour, oldest first."""

    @abstractmethod
    def allowance_remaining(
        self,
        cycle: Cycle,
        hours: Sequence[HourUsage],
        versions: Sequence[RateVersion],
    ) -> dict[str, float]:
        """Return the kWh left in each tiered period's allowance."""

    def slug_for_label(self, label: str) -> str | None:
        """Return the slug of the period the portal calls `label`."""
        folded = label.casefold()
        for period in self.periods:
            if period.label.casefold() == folded:
                return period.slug
        return None

    def newest_published_version(self) -> RateVersion:
        """Return the newest version the package ships for this tariff."""
        return max(self.published_versions, key=lambda one: one.effective_from)


def nominal_hours(cycle: Cycle) -> int:
    """Return how many hours the cycle's calendar span holds.

    Real elapsed hours, so the cycle that contains a daylight saving transition
    is 23 or 25 hours longer than its neighbours and the smear follows.
    """
    span = cycle.end.astimezone(UTC) - cycle.start.astimezone(UTC)
    return round(span.total_seconds() / HOUR_SECONDS)


def version_for(versions: Sequence[RateVersion], day: date) -> RateVersion | None:
    """Return the newest version in effect on a local calendar day."""
    governing = [one for one in versions if one.effective_from <= day]
    if not governing:
        return None
    return max(governing, key=lambda one: one.effective_from)


def rate_version_to_data(version: RateVersion) -> dict[str, Any]:
    """Return a rate version in the shape config entry data stores."""
    return {
        "effective_from": version.effective_from.isoformat(),
        "on_peak_rate": version.on_peak_rate,
        "off_peak_rate": version.off_peak_rate,
        "super_off_peak_rate": version.super_off_peak_rate,
        "super_off_peak_allowance": version.super_off_peak_allowance,
        "service_charge": version.service_charge,
        "pca_factor": version.pca_factor,
        "sales_tax_rate": version.sales_tax_rate,
        "recurring_adjustment": version.recurring_adjustment,
    }


def rate_version_from_data(data: dict[str, Any]) -> RateVersion:
    """Return the rate version one config entry record describes."""
    return RateVersion(
        effective_from=date.fromisoformat(data["effective_from"]),
        on_peak_rate=float(data["on_peak_rate"]),
        off_peak_rate=float(data["off_peak_rate"]),
        super_off_peak_rate=float(data["super_off_peak_rate"]),
        super_off_peak_allowance=float(data["super_off_peak_allowance"]),
        service_charge=float(data["service_charge"]),
        pca_factor=float(data["pca_factor"]),
        sales_tax_rate=float(data["sales_tax_rate"]),
        recurring_adjustment=float(data["recurring_adjustment"]),
    )
