"""Async client for the NISC SmartHub portal's undocumented JSON API.

The authentication and usage-poll endpoints, and the shape of the requests they
want, were proved by gagata/ha-smarthub-energy-sensor (MIT):
https://github.com/gagata/ha-smarthub-energy-sensor. This is a reimplementation
of that knowledge, not a fork.

No Home Assistant imports belong in this package.
"""

import asyncio
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, tzinfo
import logging
from typing import Any, Final

import aiohttp
import pyotp

from .models import (
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

_LOGGER = logging.getLogger(__name__)

POLL_ATTEMPTS: Final = 5
POLL_DELAY: Final = 4.0
REQUEST_TIMEOUT: Final = 30.0

MFA_FAILURE_MARKER: Final = "could not be verified"


class ClientError(Exception):
    """The portal could not be reached, or answered with something unusable."""


class AuthError(ClientError):
    """The portal rejected the credentials, the TOTP code, or the token."""


class PollTimeout(ClientError):
    """The usage poll never left PENDING."""


class UnsupportedAccount(ClientError):
    """The account has a shape this client will not guess at.

    Raised past `_parse`'s conforming errors, so a caller can tell "this will
    never work" from "the portal answered oddly this time".
    """


class MultipleProviders(UnsupportedAccount):
    """The account is billed by more than one provider."""


class MultipleMeters(UnsupportedAccount):
    """The service location has more than one meter."""


class SmartHubClient:
    """One authenticated conversation with one co-op's SmartHub portal.

    The session is injected and never closed here. `host` is the portal's
    origin, over HTTPS unless it spells another scheme out. `local_zone` is
    the portal's own timezone, which is how its usage timestamps are read (see
    `_wall`).
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        local_zone: tzinfo,
    ) -> None:
        """Initialize the client against a co-op portal host."""
        self._session = session
        origin = host.rstrip("/")
        if not origin.startswith(("https://", "http://")):
            origin = f"https://{origin}"
        self._origin = origin
        self._local_zone = local_zone
        self._auth: Session | None = None

    @property
    def session(self) -> Session:
        """Return the authenticated session, or fail if there is none yet."""
        if self._auth is None:
            raise ClientError("client is not authenticated; call login first")
        return self._auth

    def _url(self, path: str) -> str:
        return f"{self._origin}/services/{path}"

    def _headers(self) -> dict[str, str]:
        auth = self.session
        return {
            "Authorization": f"Bearer {auth.token}",
            "Content-Type": "application/json",
            "X-Nisc-Smarthub-Username": auth.username,
        }

    async def login(self, email: str, password: str, totp_secret: str) -> Session:
        """Authenticate and retain the token for subsequent calls."""
        payload = {
            "userId": email,
            "password": password,
            "twoFactorCode": pyotp.TOTP(totp_secret).now(),
        }
        status, body, data = await self._request(
            "authentication",
            "POST",
            "oauth/auth/v2",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if status == 401:
            raise AuthError("the portal rejected the credentials")
        # A missing or stale TOTP code comes back as a 500 carrying this
        # sentence, not as a 401, and retrying it never helps.
        if status == 500 and MFA_FAILURE_MARKER in body:
            raise AuthError("the portal could not verify the two-factor code")
        if status != 200 or data is None:
            raise ClientError(f"authentication failed with HTTP status {status}")

        auth = _parse(_session, data, "authentication")
        self._auth = auth
        return auth

    async def user_data(self) -> list[Customer]:
        """Return the customers, accounts, and service locations of this login."""
        data = await self._get(
            "secured/user-data",
            "user-data",
            params={"userId": self.session.primary_username},
        )
        return [_parse(_customer, entry, "user-data") for entry in data]

    async def billing(self, account: str) -> BillingSummary:
        """Return the billing facts for one account."""
        data = await self._get(
            "secured/billing",
            "billing",
            params={"userId": self.session.username, "accountNumber": account},
        )
        return _parse(lambda raw: _billing(raw, account), data, "billing")

    async def service_location(self, location: str) -> ServiceLocation:
        """Return the premise record behind a service location id."""
        data = await self._get(
            f"secured/service-locations/{location}", "service-locations"
        )
        return _parse(_service_location, data, "service-locations")

    async def poll_hourly(
        self, account: str, location: str, start: datetime, end: datetime
    ) -> PollResult:
        """Poll one hour-resolution usage window."""
        return await self._poll("HOURLY", account, location, start, end)

    async def poll_daily(
        self, account: str, location: str, start: datetime, end: datetime
    ) -> PollResult:
        """Poll one day-resolution usage window."""
        return await self._poll("DAILY", account, location, start, end)

    async def _request(
        self, endpoint: str, method: str, path: str, **kwargs: Any
    ) -> tuple[int, str, Any | None]:
        """Make one request and return its status, body, and JSON when it is 200.

        `endpoint` names the call in errors. The path can carry a service
        location id, the query the account number and the email, and aiohttp
        spells the URL into every exception it raises, so a transport failure,
        a body that is not JSON included, is reported by its type alone and
        raised outside the handler, where neither the cause nor the context
        can carry the original along.
        """
        try:
            async with (
                asyncio.timeout(REQUEST_TIMEOUT),
                self._session.request(method, self._url(path), **kwargs) as response,
            ):
                status = response.status
                body = await response.text()
                data = await response.json() if status == 200 else None
                return status, body, data
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            failure = _failure(endpoint, err)
        raise ClientError(failure)

    async def _get(
        self, path: str, endpoint: str, params: dict[str, str] | None = None
    ) -> Any:
        """Fetch one secured endpoint."""
        status, _, data = await self._request(
            endpoint, "GET", path, headers=self._headers(), params=params
        )
        if status == 401:
            raise AuthError(f"the portal rejected the token during {endpoint}")
        if status != 200:
            raise ClientError(f"{endpoint} failed with HTTP status {status}")
        return data

    async def _poll(
        self,
        time_frame: str,
        account: str,
        location: str,
        start: datetime,
        end: datetime,
    ) -> PollResult:
        body = {
            "timeFrame": time_frame,
            "userId": self.session.username,
            "screen": "USAGE_EXPLORER",
            "includeDemand": False,
            "serviceLocationNumber": location,
            "accountNumber": account,
            "industries": ["ELECTRIC"],
            "startDateTime": str(int(start.timestamp()) * 1000),
            "endDateTime": str(int(end.timestamp()) * 1000),
        }

        for attempt in range(1, POLL_ATTEMPTS + 1):
            status, _, data = await self._request(
                "poll",
                "POST",
                "secured/utility-usage/poll",
                headers=self._headers(),
                json=body,
            )
            if status == 401:
                raise AuthError("the portal rejected the token during the poll")
            if status != 200 or data is None:
                raise ClientError(f"poll failed with HTTP status {status}")

            poll_status = data["status"]
            if poll_status == "COMPLETE":
                result = _parse(
                    lambda raw: self._poll_result(raw, time_frame), data, "poll"
                )
                return _within(result, start, end)
            if poll_status != "PENDING":
                raise ClientError(f"poll answered with status {poll_status}")

            _LOGGER.debug("poll attempt %d of %d is pending", attempt, POLL_ATTEMPTS)
            if attempt < POLL_ATTEMPTS:
                await asyncio.sleep(POLL_DELAY)

        raise PollTimeout(f"poll still pending after {POLL_ATTEMPTS} attempts")

    def _wall(self, epoch_ms: int) -> datetime:
        """Read one SmartHub interval timestamp as the portal's wall clock.

        The portal's `x` values are the portal's local wall clock labeled as UTC,
        not real instants. Verified against a 2026-09-01 through 2026-09-03 pull
        for this account: the DAILY series lands on 00:00Z per day rather than
        04:00Z, and the TIME_OF_USE split puts Super Off Pk on hours 0 through 5,
        On Peak on 13 through 20, and Off Peak on the rest, which is NiteFlex's
        classification stated in local time. Read as real UTC, every boundary
        would sit four hours off.

        The request bounds do not share the quirk. Two pulls with bounds four
        hours apart came back shifted by exactly those four hours, so
        `startDateTime` and `endDateTime` are ordinary epochs. Neither does
        `billing`'s connectDate, which is a real instant at local midnight.
        """
        return datetime.fromtimestamp(epoch_ms / 1000, UTC).replace(tzinfo=None)

    def _instant(self, wall: datetime, *, fold: int = 0) -> datetime:
        """Return the real UTC instant a wall-clock reading means.

        On the night clocks fall back the wall clock repeats an hour, and
        `fold` picks which of the two instants a repeated reading means. How
        the live portal encodes that night is unverified until 2026-11-01; the
        working assumption is two points with the same wall-clock hour, in
        order, and `_intervals` reads them that way.
        """
        return wall.replace(tzinfo=self._local_zone, fold=fold).astimezone(UTC)

    def _exists(self, wall: datetime) -> bool:
        """Say whether the zone's clock ever shows a wall-clock reading."""
        return (
            self._instant(wall).astimezone(self._local_zone).replace(tzinfo=None)
            == wall
        )

    def _poll_result(self, data: Any, time_frame: str) -> PollResult:
        entries = data["data"]["ELECTRIC"]
        usage_entry = _entry(entries, "USAGE")
        meters = usage_entry["meters"]
        if len(meters) != 1:
            raise MultipleMeters(
                f"expected exactly one meter at the service location, got {len(meters)}"
            )
        meter = str(meters[0]["seriesId"])
        usage = self._intervals(
            point
            for series in usage_entry["series"]
            if series["name"] == meter
            for point in series["data"]
        )

        # The portal classifies nothing until it starts classifying, so a poll
        # with no TIME_OF_USE entry at all is a poll with nothing classified.
        tou_entry = _entry(entries, "TIME_OF_USE", required=False)
        periods: dict[str, dict[datetime, float]] = {}
        if tou_entry is not None:
            periods = {
                _period_label(series["name"], meter): self._intervals(series["data"])
                for series in tou_entry["series"]
            }
        # A day holds hours of every period; only an hour belongs to one.
        if time_frame == "HOURLY":
            _one_period_per_hour(periods)
        return PollResult(meter=meter, usage=usage, periods=periods)

    def _intervals(self, points: Iterable[Any]) -> dict[datetime, float]:
        """Return one value per interval start, folded onto the hour.

        The portal emits a stray point off the hour on the day the meter was
        connected, carrying 0.0 kWh next to that hour's own point. Adding it to
        the hour that contains it keeps whatever energy it reports without
        inventing an interval the recorder would reject.

        A wall-clock hour the zone skips on the night clocks spring forward is
        a phantom: it means the same instant as the hour after it. An empty
        phantom is dropped and a phantom carrying energy is added to that
        hour, since the portal has nowhere else to put it.

        A second top-of-hour point for a wall-clock hour is the repeated hour
        of the fall-back night and is read as its second instant. Anywhere
        else, two points for one hour mean the response is not what this
        client understands.
        """
        values: dict[datetime, float] = {}
        on_the_hour: set[datetime] = set()
        for point in points:
            wall = self._wall(point["x"])
            value = float(point["y"])
            start = self._instant(wall)
            hour = start.replace(minute=0, second=0, microsecond=0)
            if not self._exists(wall):
                if value == 0.0:
                    _LOGGER.debug(
                        "dropping the empty reading at %s, a wall-clock hour the "
                        "zone skips",
                        wall,
                    )
                    continue
                _LOGGER.debug(
                    "adding the reading at %s, a wall-clock hour the zone skips, "
                    "to the hour at %s",
                    wall,
                    hour.isoformat(),
                )
            elif hour != start:
                _LOGGER.debug("folding the point at %s into its hour", start)
            elif hour in on_the_hour:
                repeated = self._instant(wall, fold=1)
                if repeated == hour or repeated in on_the_hour:
                    raise ValueError(f"two readings for the hour at {hour.isoformat()}")
                hour = repeated
                on_the_hour.add(hour)
            else:
                on_the_hour.add(hour)
            values[hour] = values.get(hour, 0.0) + value
        return values


def _failure(endpoint: str, err: Exception) -> str:
    """Describe a transport failure by the endpoint and the failure's type."""
    described = f"{endpoint}: {type(err).__name__}"
    if isinstance(err, aiohttp.ClientResponseError):
        described += f" (HTTP status {err.status})"
    return described


def _parse[T](conform: Callable[[Any], T], data: Any, what: str) -> T:
    try:
        return conform(data)
    except (KeyError, IndexError, TypeError, ValueError) as err:
        raise ClientError(f"unexpected {what} response shape: {err!r}") from err


def _entry(entries: Any, entry_type: str, *, required: bool = True) -> Any:
    for entry in entries:
        if entry["type"] == entry_type:
            return entry
    if required:
        raise ValueError(f"no {entry_type} entry in poll response")
    return None


def _period_label(series_name: str, meter: str) -> str:
    label = series_name.removeprefix(f"{meter} - ")
    if label == series_name:
        raise ValueError(
            f"time-of-use series {series_name.replace(meter, '<meter>')!r} "
            "is not prefixed by the meter"
        )
    return label


def _one_period_per_hour(periods: dict[str, dict[datetime, float]]) -> None:
    seen: dict[datetime, str] = {}
    for label, values in periods.items():
        for hour in values:
            if hour in seen:
                raise ValueError(
                    f"the hour at {hour.isoformat()} is classified as both "
                    f"{seen[hour]!r} and {label!r}"
                )
            seen[hour] = label


def _within(result: PollResult, start: datetime, end: datetime) -> PollResult:
    """Keep the intervals inside the window the poll asked for.

    A period left with no hours is dropped with them: a label is only worth
    reporting, as unknown or otherwise, when an hour in the window carries it.
    """
    dropped = sum(1 for hour in result.usage if not start <= hour < end) + sum(
        1
        for values in result.periods.values()
        for hour in values
        if not start <= hour < end
    )
    if dropped:
        _LOGGER.warning(
            "the poll returned %d points outside the %s to %s window it was asked "
            "for; they were dropped",
            dropped,
            start.isoformat(),
            end.isoformat(),
        )
    periods = {
        label: {hour: v for hour, v in values.items() if start <= hour < end}
        for label, values in result.periods.items()
    }
    return PollResult(
        meter=result.meter,
        usage={hour: v for hour, v in result.usage.items() if start <= hour < end},
        periods={label: values for label, values in periods.items() if values},
    )


def _session(data: Any) -> Session:
    # A wrong password is answered with HTTP 200 and the body
    # `{"status": "FAILURE", "isBusinessUser": false}`, so a response with no
    # token in it is a rejection rather than a shape this cannot read.
    token = data.get("authorizationToken")
    if not token:
        raise AuthError("the portal returned no authorization token")
    return Session(
        token=token,
        username=data["username"],
        primary_username=data["primaryUsername"],
        expiration=datetime.fromtimestamp(data["expiration"], UTC),
        expires_in=data["expiresIn"],
    )


def _address(data: Any, line_key: str, zip_key: str) -> Address:
    return Address(
        line_one=data[line_key],
        city=data["city"],
        state=data["state"],
        zip_code=data[zip_key],
    )


def _customer(data: Any) -> Customer:
    locations = tuple(
        ServiceLocationSummary(
            id=location_id,
            location=summary["location"],
            address=_address(summary["address"], "addr1", "zip"),
            service_status=summary["serviceStatus"],
            services=tuple(summary["services"]),
            active_rate_schedules=tuple(summary["activeRateSchedules"]),
        )
        for location_id, summaries in data[
            "serviceLocationToUserDataServiceLocationSummaries"
        ].items()
        for summary in summaries
    )
    return Customer(
        account=data["account"],
        customer_name=data["customerName"],
        address=data["address"],
        email=data["email"],
        inactive=data["inactive"],
        primary_service_location_id=data["primaryServiceLocationId"],
        services=tuple(data["services"]),
        locations=locations,
    )


def _billing(data: Any, account: str) -> BillingSummary:
    # Indexing the map directly would put the account number in the KeyError,
    # and the error ends up in logs.
    account_billing = data[0]["accountBillingMap"].get(account)
    if account_billing is None:
        raise ValueError("the requested account is not in the billing map")
    provider = _sole(account_billing["serviceBillingMap"]["ELEC"]["providerBillingMap"])
    locations = tuple(
        BillingLocation(
            id=location_id,
            description=location["description"],
            taxable=location["taxable"],
            meters=tuple(
                Meter(
                    number=meter["id"],
                    rate_schedule=meter["rateSchedule"],
                    meter_type=meter["meterType"],
                    status=meter["status"],
                )
                for meter in location["meters"].values()
            ),
        )
        for location_id, location in provider["serviceLocationMap"].items()
    )
    return BillingSummary(
        account=account,
        connect_date=datetime.fromtimestamp(provider["connectDate"] / 1000, UTC),
        billing_cycle=account_billing["billingCycle"],
        number_of_bills=account_billing["numberOfBills"],
        has_unbilled_usage=account_billing["hasUnbilledUsage"],
        revenue_class=provider["revenueClass"],
        primary_rate_schedule=provider["primaryRateSchedule"],
        primary_rate_schedule_id=provider["primaryRateScheduleId"],
        locations=locations,
    )


def _sole(mapping: Any) -> Any:
    values = list(mapping.values())
    if len(values) != 1:
        raise MultipleProviders(f"expected exactly one provider, got {len(values)}")
    return values[0]


def _service_location(data: Any) -> ServiceLocation:
    return ServiceLocation(
        id=data["id"],
        service_description=data["serviceDescription"],
        address=data["address"],
        city=data["city"],
        state=data["state"],
        zip_code=data["zipCode"],
    )
