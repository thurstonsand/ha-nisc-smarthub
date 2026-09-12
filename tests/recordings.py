"""Conformed models matching the recorded fixtures.

Tests that mock the client hand these back instead of raw JSON, so what the
coordinator and the config flow see is exactly the shape the client produces.
The hourly usage is read out of the recorded poll so the totals stay real.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from custom_components.nisc_smarthub.smarthub import (
    Address,
    BillingLocation,
    BillingSummary,
    Customer,
    Meter,
    PollResult,
    ServiceLocation,
    ServiceLocationSummary,
    Session,
)

FIXTURES: Final = Path(__file__).parent / "fixtures"

HOST: Final = "example.smarthub.coop"
EMAIL: Final = "member@example.com"
PASSWORD: Final = "hunter2"
TOTP_SECRET: Final = "JBSWY3DPEHPK3PXP"
ACCOUNT: Final = "900000001"
LOCATION: Final = "70001"
SECOND_LOCATION: Final = "70002"
METER: Final = "1N0000000001"
DESCRIPTION: Final = "Example Premise"

PORTAL_ZONE: Final = ZoneInfo("America/New_York")
CONNECT_DATE: Final = datetime(2026, 8, 28, 4, tzinfo=UTC)

USAGE_STATISTIC_ID: Final = f"nisc_smarthub:{ACCOUNT}_{LOCATION}_usage"
COST_STATISTIC_ID: Final = f"nisc_smarthub:{ACCOUNT}_{LOCATION}_cost"
OFF_PEAK_STATISTIC_ID: Final = f"{USAGE_STATISTIC_ID}_off_peak"
ON_PEAK_STATISTIC_ID: Final = f"{USAGE_STATISTIC_ID}_on_peak"
SUPER_OFF_PEAK_STATISTIC_ID: Final = f"{USAGE_STATISTIC_ID}_super_off_peak"


def payload(name: str) -> Any:
    """Load one recorded response."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _points(series: Any) -> dict[datetime, float]:
    return {
        datetime.fromtimestamp(point["x"] / 1000, UTC)
        .replace(tzinfo=PORTAL_ZONE)
        .astimezone(UTC): float(point["y"])
        for point in series["data"]
    }


def _entry(entry_type: str) -> Any:
    entries = payload("poll_hourly")["data"]["ELECTRIC"]
    return next(entry for entry in entries if entry["type"] == entry_type)


def hourly_usage() -> dict[datetime, float]:
    """Return the recorded three-day window, keyed by aware UTC hour."""
    series = next(one for one in _entry("USAGE")["series"] if one["name"] == METER)
    return _points(series)


def hourly_periods() -> dict[str, dict[datetime, float]]:
    """Return the recorded window as the utility classified it."""
    return {
        str(series["name"]).removeprefix(f"{METER} - "): _points(series)
        for series in _entry("TIME_OF_USE")["series"]
    }


def periods_without(hours: Iterable[datetime]) -> dict[str, dict[datetime, float]]:
    """Return the recorded classification with some hours never classified."""
    dropped = set(hours)
    return {
        label: {hour: value for hour, value in values.items() if hour not in dropped}
        for label, values in hourly_periods().items()
    }


def poll_result(
    usage: dict[datetime, float] | None = None,
    periods: dict[str, dict[datetime, float]] | None = None,
) -> PollResult:
    """Return a poll carrying the recorded hours, or the ones given.

    Hours given without a classification are all off peak, which is the
    simplest thing that prices.
    """
    if usage is None:
        return PollResult(
            meter=METER,
            usage=hourly_usage(),
            periods=hourly_periods() if periods is None else periods,
        )
    return PollResult(
        meter=METER,
        usage=usage,
        periods={"Off Peak": usage} if periods is None else periods,
    )


def session() -> Session:
    """Return an authenticated session."""
    return Session(
        token="test-authorization-token",
        username=EMAIL,
        primary_username=EMAIL,
        expiration=datetime(2026, 9, 11, 12, tzinfo=UTC),
        expires_in=299,
    )


def _summary(location: str) -> ServiceLocationSummary:
    return ServiceLocationSummary(
        id=location,
        location="EXAMPLE",
        address=Address(
            line_one="100 Example Rd", city="Springfield", state="GA", zip_code="30000"
        ),
        service_status="ACTIVE",
        services=("ELEC",),
        active_rate_schedules=("NFON:COBB", "NFOFF:COBB", "NFSOF:COBB"),
    )


def customers(*locations: str) -> list[Customer]:
    """Return one customer holding the given service locations."""
    return [
        Customer(
            account=ACCOUNT,
            customer_name="Pat Member",
            address="100 Example Rd",
            email=EMAIL,
            inactive=False,
            primary_service_location_id=LOCATION,
            services=("ELEC",),
            locations=tuple(_summary(location) for location in locations),
        )
    ]


def billing_summary() -> BillingSummary:
    """Return the account's billing facts, connect date included."""
    return BillingSummary(
        account=ACCOUNT,
        connect_date=CONNECT_DATE,
        billing_cycle="3",
        number_of_bills=0,
        has_unbilled_usage=True,
        revenue_class="RESIDENTIAL",
        primary_rate_schedule="NiteFlex",
        primary_rate_schedule_id="NFON",
        locations=(
            BillingLocation(
                id=LOCATION,
                description=DESCRIPTION,
                taxable=True,
                meters=(
                    Meter(
                        number=METER,
                        rate_schedule="NFON",
                        meter_type="TIME_OF_DAY_KWH_DEMAND",
                        status="ACTIVE",
                    ),
                ),
            ),
        ),
    )


def service_location() -> ServiceLocation:
    """Return the premise record behind the service location id."""
    return ServiceLocation(
        id=LOCATION,
        service_description=DESCRIPTION,
        address="100 Example Rd",
        city="Springfield",
        state="GA",
        zip_code="30000",
    )
