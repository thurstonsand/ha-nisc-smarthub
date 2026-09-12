"""Conformed shapes of the SmartHub portal's responses.

Every field is required. Optionality here means the portal has a documented
state in which the value is genuinely absent, not that a response once lacked
the key.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, kw_only=True)
class Session:
    """An authenticated portal session.

    `expires_in` was observed at 299 seconds, so a long-running consumer
    re-authenticates rather than holding one token.
    """

    token: str
    username: str
    primary_username: str
    expiration: datetime
    expires_in: int


@dataclass(frozen=True, kw_only=True)
class Address:
    """A postal address as `user-data` and `billing` report it."""

    line_one: str
    city: str
    state: str
    zip_code: str


@dataclass(frozen=True, kw_only=True)
class ServiceLocationSummary:
    """One metered premise as it appears under a customer in `user-data`."""

    id: str
    location: str
    address: Address
    service_status: str
    services: tuple[str, ...]
    active_rate_schedules: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class Customer:
    """One customer record from `user-data`, with its service locations."""

    account: str
    customer_name: str
    address: str
    email: str
    inactive: bool
    primary_service_location_id: str
    services: tuple[str, ...]
    locations: tuple[ServiceLocationSummary, ...]


@dataclass(frozen=True, kw_only=True)
class Meter:
    """A register at a service location, as `billing` describes it."""

    number: str
    rate_schedule: str
    meter_type: str
    status: str


@dataclass(frozen=True, kw_only=True)
class BillingLocation:
    """The billing view of a service location: what it is taxed as, and its meters."""

    id: str
    description: str
    taxable: bool
    meters: tuple[Meter, ...]


@dataclass(frozen=True, kw_only=True)
class BillingSummary:
    """The account's billing facts: connect date, cycle, rate schedule, meters."""

    account: str
    connect_date: datetime
    billing_cycle: str
    number_of_bills: int
    has_unbilled_usage: bool
    revenue_class: str
    primary_rate_schedule: str
    primary_rate_schedule_id: str
    locations: tuple[BillingLocation, ...]


@dataclass(frozen=True, kw_only=True)
class ServiceLocation:
    """The premise record behind a service location id."""

    id: str
    service_description: str
    address: str
    city: str
    state: str
    zip_code: str


@dataclass(frozen=True, kw_only=True)
class PollResult:
    """One completed usage poll.

    `usage` is kWh per interval start. `periods` is the same intervals split by
    the utility's own time-of-use classification, keyed by the period label with
    the meter prefix stripped; a label with no intervals is absent. Both are
    keyed by tz-aware UTC datetimes.
    """

    meter: str
    usage: dict[datetime, float]
    periods: dict[str, dict[datetime, float]]
