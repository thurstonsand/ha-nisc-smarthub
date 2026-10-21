"""What every tariff is, and what pricing one cycle takes and returns.

A tariff is stateless: it is handed a whole cycle, the hours that arrived in it,
the rate versions that govern them, and the bill's actuals once there is a bill.
Tiering, the service-charge smear, proration, and which version governs which
hour live behind `price_cycle`, so nothing downstream carries a tariff's
vocabulary. What a tariff's rate versions and bills hold is declared as
parameters, and the shared code stores, validates, and asks for them by those
declarations alone.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from enum import StrEnum
import math
from typing import Any

HOUR_SECONDS = 3600
EFFECTIVE_FROM = "effective_from"


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
class Parameter:
    """One number a rate version or a bill hands a tariff.

    A rate, an allowance, a charge, or a tax is a quantity and cannot go
    negative; a rider or an adjustment is a signed correction.
    """

    key: str
    signed: bool


@dataclass(frozen=True, kw_only=True)
class RateVersion:
    """One dated set of a tariff's numbers, keyed by its rate parameters.

    Which hours the version governs is the tariff's rule, not the version's.
    """

    effective_from: date
    values: Mapping[str, float]


class InvalidValue(ValueError):
    """A number handed to a tariff that does not describe a price.

    `field` is the offending key and `reason` a translation key: `missing`,
    `unexpected`, `not_a_number`, or `negative_number`.
    """

    def __init__(self, field: str, reason: str) -> None:
        """Name the field and why it was refused."""
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


def conform_values(
    parameters: Sequence[Parameter], values: Mapping[str, Any]
) -> dict[str, float]:
    """Return exactly the declared parameters as finite numbers, or refuse.

    Every declared parameter is required, an undeclared key is refused, and a
    quantity has to be zero or more.
    """
    declared = {parameter.key for parameter in parameters}
    for key in values:
        if key not in declared:
            raise InvalidValue(key, "unexpected")
    conformed: dict[str, float] = {}
    for parameter in parameters:
        if parameter.key not in values:
            raise InvalidValue(parameter.key, "missing")
        try:
            value = float(values[parameter.key])
        except (TypeError, ValueError) as err:
            raise InvalidValue(parameter.key, "not_a_number") from err
        if not math.isfinite(value):
            raise InvalidValue(parameter.key, "not_a_number")
        if not parameter.signed and value < 0:
            raise InvalidValue(parameter.key, "negative_number")
        conformed[parameter.key] = value
    return conformed


@dataclass(frozen=True, kw_only=True)
class HourUsage:
    """One hour of metered usage, split the way the utility classified it.

    For a tariff priced from time of use, `by_period` is empty when the hour
    arrived in the usage series but landed in no period the tariff knows, and
    such an hour is worth kWh and no dollars. For a tariff priced from usage
    it is always empty, and `total` is what it prices.
    """

    start: datetime
    total: float
    by_period: dict[str, float]


@dataclass(frozen=True, kw_only=True)
class CycleActuals:
    """What a posted bill says about one cycle.

    The read dates and the total are every bill's; `values` holds the lines the
    tariff declares as bill parameters, which replace its estimates.
    """

    read_start: datetime
    read_end: datetime
    bill_total: float
    values: Mapping[str, float]


@dataclass(frozen=True, kw_only=True)
class CostComponents:
    """A cost split into the categories every bill's lines fall into."""

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
class PricedCycle:
    """One cycle priced: a cost per priced hour, oldest first, and what is left.

    `allowance_remaining` holds the kWh still unspent in each allowance the
    tariff declares, and is empty when nothing in the cycle was priced.
    """

    costs: list[HourCost]
    allowance_remaining: dict[str, float]


def settle(costs: Sequence[HourCost], bill_total: float) -> list[HourCost]:
    """Return a finalized cycle's costs with the residual that makes up its bill.

    The residual is smeared over the priced hours, because those are the rows
    that will exist and the bill has to add up across them.
    """
    if not costs:
        raise ValueError("a finalized cycle with no priced hours cannot carry its bill")
    residual = (bill_total - sum(cost.total for cost in costs)) / len(costs)
    return [replace(cost, residual=residual) for cost in costs]


@dataclass(frozen=True, kw_only=True)
class Tariff(ABC):
    """One rate schedule, executable.

    A location is priced under it when its portal is one of `portal_hosts` and
    its active rate schedules include every one of `rate_schedule_codes`,
    qualifier and all. A tariff priced from time of use names its periods; one
    priced from usage has none.
    """

    key: str
    display_name: str
    source: TariffSource
    periods: tuple[Period, ...]
    allowances: tuple[str, ...]
    portal_hosts: tuple[str, ...]
    rate_schedule_codes: tuple[str, ...]
    rate_parameters: tuple[Parameter, ...]
    bill_parameters: tuple[Parameter, ...]
    published_versions: tuple[RateVersion, ...]

    def __post_init__(self) -> None:
        """Refuse a definition the shared code could not price with."""
        if (self.source is TariffSource.USAGE) != (not self.periods):
            raise ValueError(
                f"{self.key}: a tariff names periods exactly when it is priced "
                "from time of use"
            )
        for version in self.published_versions:
            conform_values(self.rate_parameters, version.values)

    @abstractmethod
    def governs_cycle(self, version: RateVersion, cycle: Cycle) -> bool:
        """Say whether a version, standing alone, prices every hour of a cycle."""

    @abstractmethod
    def price_cycle(
        self,
        cycle: Cycle,
        hours: Sequence[HourUsage],
        versions: Sequence[RateVersion],
        actuals: CycleActuals | None,
    ) -> PricedCycle:
        """Price one whole cycle, with the bill's values once it has posted.

        The residual is left at zero; `settle` owns it.
        """

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

    def published_version_on(self, day: date) -> RateVersion:
        """Return the shipped version in effect on a day, or the oldest one."""
        return version_for(self.published_versions, day) or min(
            self.published_versions, key=lambda one: one.effective_from
        )

    def rate_version_from_data(self, data: Mapping[str, Any]) -> RateVersion:
        """Conform one stored or typed rate version, or refuse it."""
        values = {key: value for key, value in data.items() if key != EFFECTIVE_FROM}
        if EFFECTIVE_FROM not in data:
            raise InvalidValue(EFFECTIVE_FROM, "missing")
        return RateVersion(
            effective_from=date.fromisoformat(data[EFFECTIVE_FROM]),
            values=conform_values(self.rate_parameters, values),
        )

    def bill_values(self, values: Mapping[str, Any]) -> dict[str, float]:
        """Conform the lines a bill supplies for this tariff, or refuse them."""
        return conform_values(self.bill_parameters, values)


def rate_version_to_data(version: RateVersion) -> dict[str, Any]:
    """Return a rate version in the shape config entry data stores."""
    return {EFFECTIVE_FROM: version.effective_from.isoformat(), **version.values}


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
