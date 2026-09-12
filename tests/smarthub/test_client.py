"""Tests for the SmartHub client against recorded, scrubbed fixtures."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
import itertools
import json
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.nisc_smarthub.smarthub import (
    AuthError,
    ClientError,
    MultipleMeters,
    MultipleProviders,
    PollTimeout,
    SmartHubClient,
)

HOST = "example.smarthub.coop"
BASE = f"https://{HOST}/services"
AUTH_URL = f"{BASE}/oauth/auth/v2"
POLL_URL = f"{BASE}/secured/utility-usage/poll"
ACCOUNT = "900000001"
LOCATION = "70001"
METER = "1N0000000001"
EMAIL = "member@example.com"
TOKEN = "test-authorization-token"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"
PORTAL_ZONE = ZoneInfo("America/New_York")

FIXTURES = Path(__file__).parent.parent / "fixtures"

MFA_FAILURE_BODY = (
    "Your data could not be verified. If the problem persists, "
    "please contact customer service."
)


def fixture(name: str) -> Any:
    """Load one recorded response."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def login_response() -> dict[str, Any]:
    """Return the recorded login response with a token put back on it.

    The capture script drops the real token rather than scrubbing it, so the
    fixture has no `authorizationToken` field to record.
    """
    return fixture("login") | {"authorizationToken": TOKEN}


@pytest.fixture
async def session(
    aioclient_mock: AiohttpClientMocker,
) -> AsyncGenerator[aiohttp.ClientSession]:
    """Return a client session bound to the aiohttp mocker."""
    client_session = aioclient_mock.create_session(asyncio.get_running_loop())
    yield client_session
    await client_session.close()


@pytest.fixture
def client(session: aiohttp.ClientSession) -> SmartHubClient:
    """Return a client for the fake portal host."""
    return SmartHubClient(session, HOST, PORTAL_ZONE)


@pytest.fixture
async def logged_in(
    client: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> SmartHubClient:
    """Return a client that already holds a session."""
    aioclient_mock.post(AUTH_URL, json=login_response())
    await client.login(EMAIL, "password", TOTP_SECRET)
    aioclient_mock.clear_requests()
    return client


async def test_login_returns_a_session(
    client: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A successful login conforms the portal's token response."""
    aioclient_mock.post(AUTH_URL, json=login_response())

    session = await client.login(EMAIL, "password", TOTP_SECRET)

    assert session.token == TOKEN
    assert session.username == EMAIL
    assert session.primary_username == EMAIL
    assert session.expires_in == 299
    assert session.expiration.tzinfo is UTC
    assert client.session is session

    _, _, data, _ = aioclient_mock.mock_calls[0]
    assert data["userId"] == EMAIL
    assert data["twoFactorCode"].isdigit()


async def test_login_without_a_verified_code_is_an_auth_error(
    client: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The portal's HTTP 500 for a bad TOTP code is an auth failure, not a retry."""
    aioclient_mock.post(AUTH_URL, status=500, text=MFA_FAILURE_BODY)

    with pytest.raises(AuthError, match="two-factor"):
        await client.login(EMAIL, "password", TOTP_SECRET)


async def test_login_rejected_credentials_are_an_auth_error(
    client: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """HTTP 401 on the token endpoint is an auth failure."""
    aioclient_mock.post(AUTH_URL, status=401, text="")

    with pytest.raises(AuthError, match="credentials"):
        await client.login(EMAIL, "password", TOTP_SECRET)


async def test_login_other_server_errors_are_client_errors(
    client: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """An HTTP 500 without the verification sentence is a transient failure."""
    aioclient_mock.post(AUTH_URL, status=500, text="gateway is unwell")

    with pytest.raises(ClientError, match="HTTP status 500"):
        await client.login(EMAIL, "password", TOTP_SECRET)


async def test_login_connection_error_is_a_client_error(
    client: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A dropped connection during login is a transient failure."""
    aioclient_mock.post(AUTH_URL, exc=aiohttp.ClientConnectionError("no route"))

    with pytest.raises(ClientError, match="authentication: ClientConnectionError"):
        await client.login(EMAIL, "password", TOTP_SECRET)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(login_response() | {"authorizationToken": ""}, id="empty_token"),
        pytest.param(
            {"status": "FAILURE", "isBusinessUser": False}, id="wrong_password"
        ),
    ],
)
async def test_login_without_a_token_is_an_auth_error(
    client: SmartHubClient, aioclient_mock: AiohttpClientMocker, body: dict[str, object]
) -> None:
    """A 200 that carries no token is still a failed login.

    The live portal answers a wrong password that way, so reading it as an
    unexpected shape would retry forever instead of asking for reauth.
    """
    aioclient_mock.post(AUTH_URL, json=body)

    with pytest.raises(AuthError, match="no authorization token"):
        await client.login(EMAIL, "password", TOTP_SECRET)


async def test_calls_before_login_fail(client: SmartHubClient) -> None:
    """Nothing secured is attempted without a session."""
    with pytest.raises(ClientError, match="not authenticated"):
        await client.user_data()


async def test_user_data_conforms_customers(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """`user-data` becomes customers with their service locations."""
    aioclient_mock.get(f"{BASE}/secured/user-data", json=fixture("user_data"))

    customers = await logged_in.user_data()

    assert len(customers) == 1
    customer = customers[0]
    assert customer.account == ACCOUNT
    assert customer.inactive is False
    assert customer.primary_service_location_id == LOCATION
    assert customer.services == ("ELEC",)

    assert len(customer.locations) == 1
    location = customer.locations[0]
    assert location.id == LOCATION
    assert location.service_status == "ACTIVE"
    assert location.active_rate_schedules == ("NFON:COBB", "NFOFF:COBB", "NFSOF:COBB")
    assert location.address.state == "GA"

    _, url, _, headers = aioclient_mock.mock_calls[0]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["X-Nisc-Smarthub-Username"] == EMAIL
    assert url.query["userId"] == EMAIL


async def test_user_data_rejects_an_unexpected_shape(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A response missing a field the client needs is a client error."""
    aioclient_mock.get(f"{BASE}/secured/user-data", json=[{"account": ACCOUNT}])

    with pytest.raises(ClientError, match="unexpected user-data response shape"):
        await logged_in.user_data()


async def test_expired_token_is_an_auth_error(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """HTTP 401 on a secured call sends the caller back to authentication."""
    aioclient_mock.get(f"{BASE}/secured/user-data", status=401, text="")

    with pytest.raises(AuthError, match="rejected the token"):
        await logged_in.user_data()


async def test_secured_call_server_error_is_a_client_error(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Any other HTTP status on a secured call is transient."""
    aioclient_mock.get(f"{BASE}/secured/user-data", status=503, text="")

    with pytest.raises(ClientError, match="HTTP status 503"):
        await logged_in.user_data()


async def test_secured_call_connection_error_is_a_client_error(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A dropped connection on a secured call is transient."""
    aioclient_mock.get(
        f"{BASE}/secured/user-data", exc=aiohttp.ClientConnectionError("reset")
    )

    with pytest.raises(ClientError, match="user-data: ClientConnectionError"):
        await logged_in.user_data()


async def serve(
    session: aiohttp.ClientSession, handler: Callable[[web.Request], Any]
) -> tuple[TestServer, SmartHubClient]:
    """Stand up a portal that logs a client in and answers `billing` with `handler`."""

    async def login(_request: web.Request) -> web.Response:
        return web.json_response(login_response())

    app = web.Application()
    app.router.add_post("/services/oauth/auth/v2", login)
    app.router.add_get("/services/secured/billing", handler)
    server = TestServer(app)
    await server.start_server()
    client = SmartHubClient(session, f"http://127.0.0.1:{server.port}", PORTAL_ZONE)
    await client.login(EMAIL, "password", TOTP_SECRET)
    return server, client


def assert_carries_no_identifier(err: BaseException) -> None:
    """The message, the cause, and the context all stay free of the request."""
    assert err.__cause__ is None
    assert err.__context__ is None
    for identifier in (ACCOUNT, EMAIL, LOCATION):
        assert identifier not in str(err)


async def test_a_body_that_is_not_json_names_only_the_endpoint_and_the_failure(
    socket_enabled: None,
) -> None:
    """Aiohttp spells the URL, query and all, into the error it raises.

    A real server is what makes it raise that error, so this one is not
    mocked.
    """

    async def html(_request: web.Request) -> web.Response:
        return web.Response(text="<html>maintenance</html>", content_type="text/html")

    async with aiohttp.ClientSession() as session:
        server, client = await serve(session, html)
        try:
            with pytest.raises(ClientError) as caught:
                await client.billing(ACCOUNT)
        finally:
            await server.close()

    assert str(caught.value) == "billing: ContentTypeError (HTTP status 200)"
    assert_carries_no_identifier(caught.value)


async def test_a_request_that_hangs_times_out_as_a_client_error(
    socket_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No request waits forever on the portal."""
    monkeypatch.setattr(
        "custom_components.nisc_smarthub.smarthub.client.REQUEST_TIMEOUT", 0.05
    )

    async def hang(_request: web.Request) -> web.Response:
        await asyncio.Event().wait()
        return web.json_response({})

    async with aiohttp.ClientSession() as session:
        server, client = await serve(session, hang)
        try:
            with pytest.raises(ClientError) as caught:
                await client.billing(ACCOUNT)
        finally:
            await server.close()

    assert str(caught.value) == "billing: TimeoutError"
    assert_carries_no_identifier(caught.value)


async def test_billing_conforms_the_account(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """`billing` becomes the account's connect date, cycle, and meters."""
    aioclient_mock.get(f"{BASE}/secured/billing", json=fixture("billing"))

    summary = await logged_in.billing(ACCOUNT)

    assert summary.account == ACCOUNT
    assert summary.billing_cycle == "3"
    assert summary.number_of_bills == 0
    assert summary.revenue_class == "RESIDENTIAL"
    assert summary.primary_rate_schedule_id == "NFON"
    # connectDate is a real instant at local midnight, unlike the poll's
    # timestamps: 2026-08-28 00:00 in the portal's zone.
    assert summary.connect_date == datetime(2026, 8, 28, 4, tzinfo=UTC)

    assert len(summary.locations) == 1
    location = summary.locations[0]
    assert location.id == LOCATION
    assert location.taxable is True
    assert [meter.number for meter in location.meters] == [METER]
    assert location.meters[0].meter_type == "TIME_OF_DAY_KWH_DEMAND"


async def test_billing_with_two_providers_is_rejected(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """An account served by two providers is a shape the client will not guess at."""
    payload = fixture("billing")
    provider_map = payload[0]["accountBillingMap"][ACCOUNT]["serviceBillingMap"][
        "ELEC"
    ]["providerBillingMap"]
    provider_map["OTHER"] = provider_map["COBB"]
    aioclient_mock.get(f"{BASE}/secured/billing", json=payload)

    with pytest.raises(MultipleProviders, match="exactly one provider"):
        await logged_in.billing(ACCOUNT)


async def test_billing_for_an_account_the_map_lacks_names_no_account(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The error ends up in logs, so the account number stays out of it."""
    aioclient_mock.get(f"{BASE}/secured/billing", json=fixture("billing"))

    with pytest.raises(ClientError, match="not in the billing map") as caught:
        await logged_in.billing("123456789")

    assert "123456789" not in str(caught.value)


async def test_a_failing_secured_call_names_the_endpoint_not_the_path(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The service location id is in the path, and the path is not in the error."""
    aioclient_mock.get(
        f"{BASE}/secured/service-locations/{LOCATION}", status=503, text=""
    )

    with pytest.raises(ClientError, match="service-locations failed") as caught:
        await logged_in.service_location(LOCATION)

    assert LOCATION not in str(caught.value)


async def test_service_location_conforms_the_premise(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """`service-locations/<id>` becomes the premise record."""
    aioclient_mock.get(
        f"{BASE}/secured/service-locations/{LOCATION}", json=fixture("service_location")
    )

    location = await logged_in.service_location(LOCATION)

    assert location.id == LOCATION
    assert location.state == "GA"
    assert location.service_description == "Example Premise"


def pending_then_complete(
    complete: Any, pending_answers: int
) -> Callable[[str, Any, Any], Any]:
    """Answer PENDING a fixed number of times, then COMPLETE."""
    remaining = pending_answers

    async def respond(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        nonlocal remaining
        payload = fixture("poll_pending") if remaining > 0 else complete
        remaining -= 1
        return AiohttpClientMockResponse(method, url, json=payload)

    return respond


@pytest.fixture(autouse=True)
def no_poll_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the poll's retry delay out of the test runtime."""

    async def immediately(_delay: float) -> None:
        return None

    monkeypatch.setattr(
        "custom_components.nisc_smarthub.smarthub.client.asyncio.sleep", immediately
    )


async def test_poll_waits_for_completion(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A PENDING answer is retried until the poll completes."""
    aioclient_mock.post(
        POLL_URL, side_effect=pending_then_complete(fixture("poll_hourly"), 1)
    )

    result = await logged_in.poll_hourly(
        ACCOUNT,
        LOCATION,
        datetime(2026, 9, 1, tzinfo=PORTAL_ZONE),
        datetime(2026, 9, 4, tzinfo=PORTAL_ZONE),
    )

    assert aioclient_mock.call_count == 2
    assert result.meter == METER
    assert len(result.usage) == 72

    _, _, body, _ = aioclient_mock.mock_calls[0]
    assert body["timeFrame"] == "HOURLY"
    assert body["serviceLocationNumber"] == LOCATION
    assert body["accountNumber"] == ACCOUNT
    # The request bounds are ordinary epochs, unlike the response timestamps.
    assert body["startDateTime"] == "1788235200000"
    assert body["endDateTime"] == "1788494400000"


async def test_poll_gives_up_while_still_pending(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A poll that never completes raises, rather than returning nothing."""
    aioclient_mock.post(POLL_URL, json=fixture("poll_pending"))

    with pytest.raises(PollTimeout, match="5 attempts"):
        await logged_in.poll_hourly(
            ACCOUNT,
            LOCATION,
            datetime(2026, 9, 1, tzinfo=PORTAL_ZONE),
            datetime(2026, 9, 4, tzinfo=PORTAL_ZONE),
        )

    assert aioclient_mock.call_count == 5


async def test_poll_failed_status_is_a_client_error(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Any status other than PENDING or COMPLETE ends the poll."""
    aioclient_mock.post(POLL_URL, json={"status": "FAILED"})

    with pytest.raises(ClientError, match="status FAILED"):
        await logged_in.poll_hourly(
            ACCOUNT,
            LOCATION,
            datetime(2026, 9, 1, tzinfo=PORTAL_ZONE),
            datetime(2026, 9, 4, tzinfo=PORTAL_ZONE),
        )


async def test_poll_rejected_token_is_an_auth_error(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """HTTP 401 during the poll is an auth failure."""
    aioclient_mock.post(POLL_URL, status=401, text="")

    with pytest.raises(AuthError, match="rejected the token"):
        await logged_in.poll_hourly(
            ACCOUNT,
            LOCATION,
            datetime(2026, 9, 1, tzinfo=PORTAL_ZONE),
            datetime(2026, 9, 4, tzinfo=PORTAL_ZONE),
        )


async def test_poll_server_error_is_a_client_error(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A failing poll request is transient."""
    aioclient_mock.post(POLL_URL, status=502, text="")

    with pytest.raises(ClientError, match="HTTP status 502"):
        await logged_in.poll_hourly(
            ACCOUNT,
            LOCATION,
            datetime(2026, 9, 1, tzinfo=PORTAL_ZONE),
            datetime(2026, 9, 4, tzinfo=PORTAL_ZONE),
        )


async def test_poll_connection_error_is_a_client_error(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A dropped connection during the poll is transient."""
    aioclient_mock.post(POLL_URL, exc=aiohttp.ClientConnectionError("reset"))

    with pytest.raises(ClientError, match="poll: ClientConnectionError"):
        await logged_in.poll_hourly(
            ACCOUNT,
            LOCATION,
            datetime(2026, 9, 1, tzinfo=PORTAL_ZONE),
            datetime(2026, 9, 4, tzinfo=PORTAL_ZONE),
        )


async def poll_hourly_fixture(
    client: SmartHubClient, aioclient_mock: AiohttpClientMocker, payload: Any
) -> Any:
    """Run one hourly poll against a given payload."""
    aioclient_mock.post(POLL_URL, json=payload)
    return await client.poll_hourly(
        ACCOUNT,
        LOCATION,
        datetime(2026, 9, 1, tzinfo=PORTAL_ZONE),
        datetime(2026, 9, 4, tzinfo=PORTAL_ZONE),
    )


async def test_poll_reads_timestamps_as_portal_wall_time(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Interval timestamps are local wall time labeled UTC, and are conformed."""
    result = await poll_hourly_fixture(
        logged_in, aioclient_mock, fixture("poll_hourly")
    )

    hours = sorted(result.usage)
    assert hours[0] == datetime(2026, 9, 1, 4, tzinfo=UTC)
    assert hours[-1] == datetime(2026, 9, 4, 3, tzinfo=UTC)
    assert all(hour.tzinfo is UTC for hour in hours)
    assert all(
        later - earlier == timedelta(hours=1)
        for earlier, later in itertools.pairwise(hours)
    )


async def test_poll_folds_a_point_off_the_hour_into_its_hour(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The connect day carries a stray reading between two hourly points."""
    payload = fixture("poll_hourly")
    usage = next(
        entry for entry in payload["data"]["ELECTRIC"] if entry["type"] == "USAGE"
    )
    series = next(one for one in usage["series"] if one["name"] == METER)
    first = series["data"][0]
    series["data"].insert(1, {"x": first["x"] + 44 * 60 * 1000, "y": 0.25})

    result = await poll_hourly_fixture(logged_in, aioclient_mock, payload)

    assert len(result.usage) == 72
    assert all(hour.minute == 0 for hour in result.usage)
    assert result.usage[datetime(2026, 9, 1, 4, tzinfo=UTC)] == pytest.approx(
        first["y"] + 0.25
    )


async def test_poll_partitions_every_hour_into_one_period(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Each usage hour appears in exactly one period, at the same value."""
    result = await poll_hourly_fixture(
        logged_in, aioclient_mock, fixture("poll_hourly")
    )

    assert set(result.periods) == {"Off Peak", "On Peak", "Super Off Pk"}

    classified: dict[datetime, list[tuple[str, float]]] = {}
    for label, series in result.periods.items():
        for hour, value in series.items():
            classified.setdefault(hour, []).append((label, value))

    assert set(classified) == set(result.usage)
    assert all(len(entries) == 1 for entries in classified.values())
    assert all(
        abs(result.usage[hour] - entries[0][1]) < 1e-9
        for hour, entries in classified.items()
    )
    assert abs(sum(result.usage.values()) - _period_total(result)) < 1e-9


def _period_total(result: Any) -> float:
    return sum(value for series in result.periods.values() for value in series.values())


async def test_poll_periods_follow_the_local_clock(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Super Off Pk covers local midnight to 6am, which is how NiteFlex states it."""
    result = await poll_hourly_fixture(
        logged_in, aioclient_mock, fixture("poll_hourly")
    )

    local_hours = {
        label: {hour.astimezone(PORTAL_ZONE).hour for hour in series}
        for label, series in result.periods.items()
    }
    assert local_hours["Super Off Pk"] == {0, 1, 2, 3, 4, 5}
    assert local_hours["On Peak"] == {13, 14, 15, 16, 17, 18, 19, 20}
    assert local_hours["Off Peak"] == {6, 7, 8, 9, 10, 11, 12, 21, 22, 23}


async def test_daily_poll_totals_match_the_hourly_days(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A daily point is the sum of that local day's hours, timestamped the same way."""
    aioclient_mock.post(POLL_URL, json=fixture("poll_daily"))
    daily = await logged_in.poll_daily(
        ACCOUNT,
        LOCATION,
        datetime(2026, 9, 1, tzinfo=PORTAL_ZONE),
        datetime(2026, 9, 4, tzinfo=PORTAL_ZONE),
    )
    aioclient_mock.clear_requests()
    hourly = await poll_hourly_fixture(
        logged_in, aioclient_mock, fixture("poll_hourly")
    )

    per_day: dict[Any, float] = {}
    for hour, value in hourly.usage.items():
        local_day = hour.astimezone(PORTAL_ZONE).date()
        per_day[local_day] = per_day.get(local_day, 0.0) + value

    assert len(daily.usage) == 3
    for start, total in daily.usage.items():
        assert start.astimezone(PORTAL_ZONE).hour == 0
        # The portal rounds each series to two decimals independently, so the
        # day and its hours disagree by a few hundredths. A day read four hours
        # off would disagree by kilowatt-hours.
        assert abs(per_day[start.astimezone(PORTAL_ZONE).date()] - total) < 0.05


async def test_poll_requires_a_prefixed_period_label(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A time-of-use series that is not named after the meter is unusable."""
    payload = fixture("poll_hourly")
    for entry in payload["data"]["ELECTRIC"]:
        if entry["type"] == "TIME_OF_USE":
            entry["series"][0]["name"] = "Off Peak"

    with pytest.raises(ClientError, match="not prefixed by the meter"):
        await poll_hourly_fixture(logged_in, aioclient_mock, payload)


def usage_only(payload: Any) -> Any:
    """Return the poll answer with its classification removed."""
    payload["data"]["ELECTRIC"] = [
        entry for entry in payload["data"]["ELECTRIC"] if entry["type"] == "USAGE"
    ]
    return payload


async def test_poll_without_a_time_of_use_entry_has_nothing_classified(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The portal classifies nothing before it starts, and that is not an error."""
    result = await poll_hourly_fixture(
        logged_in, aioclient_mock, usage_only(fixture("poll_hourly"))
    )

    assert len(result.usage) == 72
    assert result.periods == {}


async def test_poll_with_a_malformed_time_of_use_entry_is_rejected(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A classification that is present and unreadable is a client error."""
    payload = fixture("poll_hourly")
    for entry in payload["data"]["ELECTRIC"]:
        if entry["type"] == "TIME_OF_USE":
            del entry["series"]

    with pytest.raises(ClientError, match="unexpected poll response shape"):
        await poll_hourly_fixture(logged_in, aioclient_mock, payload)


async def test_poll_with_an_hour_in_two_periods_is_rejected(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Every hour lands in exactly one period; a second one is unreadable."""
    payload = fixture("poll_hourly")
    tou = next(
        entry for entry in payload["data"]["ELECTRIC"] if entry["type"] == "TIME_OF_USE"
    )
    tou["series"][1]["data"].append(tou["series"][0]["data"][0])

    with pytest.raises(ClientError, match="classified as both"):
        await poll_hourly_fixture(logged_in, aioclient_mock, payload)


async def test_poll_with_two_meters_is_unsupported(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A second meter at the location is a shape this client will not guess at."""
    payload = fixture("poll_hourly")
    usage = next(
        entry for entry in payload["data"]["ELECTRIC"] if entry["type"] == "USAGE"
    )
    usage["meters"].append(dict(usage["meters"][0], seriesId="1N0000000002"))

    with pytest.raises(MultipleMeters, match="got 2"):
        await poll_hourly_fixture(logged_in, aioclient_mock, payload)


async def test_poll_drops_points_outside_the_window_it_asked_for(
    logged_in: SmartHubClient,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only hours inside the requested window are returned, and dropping is loud."""
    aioclient_mock.post(POLL_URL, json=fixture("poll_hourly"))

    with caplog.at_level(
        logging.WARNING, "custom_components.nisc_smarthub.smarthub.client"
    ):
        result = await logged_in.poll_hourly(
            ACCOUNT,
            LOCATION,
            datetime(2026, 9, 2, tzinfo=PORTAL_ZONE),
            datetime(2026, 9, 3, tzinfo=PORTAL_ZONE),
        )

    assert len(result.usage) == 24
    assert all(hour.astimezone(PORTAL_ZONE).day == 2 for hour in result.usage)
    assert all(
        hour.astimezone(PORTAL_ZONE).day == 2
        for values in result.periods.values()
        for hour in values
    )
    # 48 usage points and their 48 classified twins.
    assert "returned 96 points outside" in caplog.text


async def test_a_period_with_no_hours_in_the_window_is_dropped_with_them(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A label is only reported when an hour in the window carries it."""
    payload = fixture("poll_hourly")
    tou = next(
        entry for entry in payload["data"]["ELECTRIC"] if entry["type"] == "TIME_OF_USE"
    )
    outside = datetime(2026, 9, 5, 12).replace(tzinfo=UTC)
    tou["series"].append(
        {
            "name": f"{METER} - Shoulder",
            "data": [{"x": int(outside.timestamp() * 1000), "y": 1.0}],
        }
    )

    result = await poll_hourly_fixture(logged_in, aioclient_mock, payload)

    assert set(result.periods) == {"Off Peak", "On Peak", "Super Off Pk"}


def synthetic_poll(local_points: list[tuple[datetime, float]]) -> Any:
    """Return a COMPLETE poll answer carrying given local wall-clock points.

    The portal labels its wall clock as UTC, so each point's `x` is the wall
    time's epoch as if it were UTC.
    """
    data = [
        {"x": int(wall.replace(tzinfo=UTC).timestamp() * 1000), "y": value}
        for wall, value in local_points
    ]
    return {
        "status": "COMPLETE",
        "data": {
            "ELECTRIC": [
                {
                    "type": "USAGE",
                    "meters": [{"seriesId": METER}],
                    "series": [{"name": METER, "data": data}],
                },
                {
                    "type": "TIME_OF_USE",
                    "series": [{"name": f"{METER} - Super Off Pk", "data": data}],
                },
            ]
        },
    }


async def test_the_repeated_hour_of_the_fall_back_night_is_read_in_order(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Two 1am points on the fall-back night are its first and second instants.

    How the live portal encodes that night is unverified until 2026-11-01;
    this pins the working assumption.
    """
    night = datetime(2026, 11, 1)
    aioclient_mock.post(
        POLL_URL,
        json=synthetic_poll(
            [
                (night.replace(hour=0), 1.0),
                (night.replace(hour=1), 2.0),
                (night.replace(hour=1), 3.0),
                (night.replace(hour=2), 4.0),
            ]
        ),
    )

    result = await logged_in.poll_hourly(
        ACCOUNT,
        LOCATION,
        datetime(2026, 11, 1, tzinfo=PORTAL_ZONE),
        datetime(2026, 11, 2, tzinfo=PORTAL_ZONE),
    )

    hours = sorted(result.usage)
    assert hours == [
        datetime(2026, 11, 1, 4, tzinfo=UTC),
        datetime(2026, 11, 1, 5, tzinfo=UTC),
        datetime(2026, 11, 1, 6, tzinfo=UTC),
        datetime(2026, 11, 1, 7, tzinfo=UTC),
    ]
    assert [result.usage[hour] for hour in hours] == [1.0, 2.0, 3.0, 4.0]
    assert result.periods["Super Off Pk"] == result.usage


async def test_the_spring_forward_night_skips_an_hour(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The night has no 2am, and the portal's points step straight over it."""
    night = datetime(2026, 3, 8)
    aioclient_mock.post(
        POLL_URL,
        json=synthetic_poll(
            [
                (night.replace(hour=0), 1.0),
                (night.replace(hour=1), 2.0),
                (night.replace(hour=3), 3.0),
                (night.replace(hour=4), 4.0),
            ]
        ),
    )

    result = await logged_in.poll_hourly(
        ACCOUNT,
        LOCATION,
        datetime(2026, 3, 8, tzinfo=PORTAL_ZONE),
        datetime(2026, 3, 9, tzinfo=PORTAL_ZONE),
    )

    hours = sorted(result.usage)
    assert hours == [
        datetime(2026, 3, 8, 5, tzinfo=UTC),
        datetime(2026, 3, 8, 6, tzinfo=UTC),
        datetime(2026, 3, 8, 7, tzinfo=UTC),
        datetime(2026, 3, 8, 8, tzinfo=UTC),
    ]


async def test_a_duplicate_hour_on_an_ordinary_night_is_rejected(
    logged_in: SmartHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A second reading for an hour that does not repeat is unreadable."""
    night = datetime(2026, 9, 1)
    aioclient_mock.post(
        POLL_URL,
        json=synthetic_poll(
            [
                (night.replace(hour=1), 1.0),
                (night.replace(hour=2), 2.0),
                (night.replace(hour=2), 3.0),
            ]
        ),
    )

    with pytest.raises(ClientError, match="two readings for the hour"):
        await logged_in.poll_hourly(
            ACCOUNT,
            LOCATION,
            night.replace(tzinfo=PORTAL_ZONE),
            night.replace(tzinfo=PORTAL_ZONE) + timedelta(days=1),
        )


@pytest.mark.parametrize(
    ("phantom", "expected"),
    [
        pytest.param(0.0, 3.0, id="an_empty_phantom_is_dropped"),
        pytest.param(2.0, 5.0, id="a_phantom_carrying_energy_is_added_to_its_hour"),
    ],
)
async def test_the_hour_the_spring_forward_night_skips_is_a_phantom(
    logged_in: SmartHubClient,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    phantom: float,
    expected: float,
) -> None:
    """The zone has no 2am that night, so a 2am reading means the 3am instant."""
    night = datetime(2026, 3, 8)
    aioclient_mock.post(
        POLL_URL,
        json=synthetic_poll(
            [
                (night.replace(hour=1), 1.0),
                (night.replace(hour=2), phantom),
                (night.replace(hour=3), 3.0),
            ]
        ),
    )

    with caplog.at_level(
        logging.DEBUG, "custom_components.nisc_smarthub.smarthub.client"
    ):
        result = await logged_in.poll_hourly(
            ACCOUNT,
            LOCATION,
            night.replace(tzinfo=PORTAL_ZONE),
            night.replace(tzinfo=PORTAL_ZONE) + timedelta(days=1),
        )

    assert result.usage == {
        datetime(2026, 3, 8, 6, tzinfo=UTC): 1.0,
        datetime(2026, 3, 8, 7, tzinfo=UTC): expected,
    }
    assert "a wall-clock hour the zone skips" in caplog.text
