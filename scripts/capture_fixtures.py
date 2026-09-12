"""Record scrubbed SmartHub fixtures from the live portal.

Credentials arrive in the environment from `fnox exec`, which resolves the
`op://agent/...` references in `fnox.toml` through the agent-vault service
account, and are never printed or written anywhere. Every response is scrubbed twice. First by value: the account
numbers, service location ids, meter numbers, emails, names, and addresses of
every account and location the login can see are harvested from `user-data`
and replaced wherever they appear, dict keys and series names included. Then
by path: a scalar survives only if its full path is allowed through, or is
given a fixed fake; every other scalar becomes a placeholder of its type, and
its path is printed so a field the portal adds later is allowed deliberately
or not at all. The script then refuses to write anything if a harvested real
value survives.

Run it through the task that supplies the credentials:

    mise run fixtures:capture

`--from-fixtures` runs the same scrub over the recorded fixtures instead of
the portal, which is how a policy change is proved without a live capture.
"""

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from typing import Final
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import aiohttp
import pyotp

type Json = str | int | float | bool | list[Json] | dict[str, Json] | None

CREDENTIAL_VARIABLES: Final = {
    "host": "SMARTHUB_HOST",
    "email": "SMARTHUB_EMAIL",
    "password": "SMARTHUB_PASSWORD",
    "totp": "SMARTHUB_TOTP_URI",
    "account": "SMARTHUB_ACCOUNT",
    "location": "SMARTHUB_LOCATION",
    "meter": "SMARTHUB_METER",
}

FIXTURES: Final = Path(__file__).parent.parent / "tests" / "fixtures"
PORTAL_ZONE: Final = ZoneInfo("America/New_York")
WINDOW_START: Final = datetime(2026, 9, 1, tzinfo=PORTAL_ZONE)
WINDOW_END: Final = datetime(2026, 9, 4, tzinfo=PORTAL_ZONE)

# Each kind of identifier is numbered in the order it is first seen, so the
# configured account, location, and meter come out as the first of each and
# the tests can name them.
FAKES: Final[dict[str, str]] = {
    "account": "9000000{n:02d}",
    "location": "700{n:02d}",
    "meter": "1N00000000{n:02d}",
    "meter_base": "90000{n:02d}",
    "email": "member{n}@example.com",
    "name": "Pat Member {n}",
    "address": "{n}00 Example Rd",
}
FIRST_FAKES: Final = {
    "email": "member@example.com",
    "name": "Pat Member",
    "address": "100 Example Rd",
}

DESCRIPTION_FAKE: Final = "Example Premise"

# Scalars the tests read by value, given a fixed fake instead of a placeholder.
FAKED_PATHS: Final[dict[str, str]] = {
    "billing[].accountBillingMap.<key>.serviceBillingMap.ELEC.providerBillingMap.COBB.serviceLocationMap.<key>.description": DESCRIPTION_FAKE,
    "billing[].accountBillingMap.<key>.serviceBillingMap.ELEC.serviceSummary.serviceDescription": DESCRIPTION_FAKE,
    "billing[].accountBillingMap.<key>.serviceLocationMap.<key>.description": DESCRIPTION_FAKE,
    "service_location.serviceDescription": DESCRIPTION_FAKE,
}  # fmt: skip

# Every scalar recorded as it arrived, after the value pass has rewritten the
# identifiers in it: what the client reads, the usage numbers and timestamps,
# and the period vocabulary. A path that is neither here nor in FAKED_PATHS is
# recorded as a placeholder and reported. Personal attributes (credit rating,
# ethnic group, medical necessity, past-due amounts, and their like) are never
# allowed through.
ALLOWED_PATHS: Final = frozenset(
    {
        "billing[].accountBillingMap.<key>.accountSummary.account",
        "billing[].accountBillingMap.<key>.billingCycle",
        "billing[].accountBillingMap.<key>.hasUnbilledUsage",
        "billing[].accountBillingMap.<key>.numberOfBills",
        "billing[].accountBillingMap.<key>.serviceBillingMap.ELEC.providerBillingMap.COBB.connectDate",
        "billing[].accountBillingMap.<key>.serviceBillingMap.ELEC.providerBillingMap.COBB.primaryRateSchedule",
        "billing[].accountBillingMap.<key>.serviceBillingMap.ELEC.providerBillingMap.COBB.primaryRateScheduleId",
        "billing[].accountBillingMap.<key>.serviceBillingMap.ELEC.providerBillingMap.COBB.revenueClass",
        "billing[].accountBillingMap.<key>.serviceBillingMap.ELEC.providerBillingMap.COBB.serviceLocationMap.<key>.address.state",
        "billing[].accountBillingMap.<key>.serviceBillingMap.ELEC.providerBillingMap.COBB.serviceLocationMap.<key>.meters.<key>.id",
        "billing[].accountBillingMap.<key>.serviceBillingMap.ELEC.providerBillingMap.COBB.serviceLocationMap.<key>.meters.<key>.meterType",
        "billing[].accountBillingMap.<key>.serviceBillingMap.ELEC.providerBillingMap.COBB.serviceLocationMap.<key>.meters.<key>.rateSchedule",
        "billing[].accountBillingMap.<key>.serviceBillingMap.ELEC.providerBillingMap.COBB.serviceLocationMap.<key>.meters.<key>.status",
        "billing[].accountBillingMap.<key>.serviceBillingMap.ELEC.providerBillingMap.COBB.serviceLocationMap.<key>.primaryRateSchedule",
        "billing[].accountBillingMap.<key>.serviceBillingMap.ELEC.providerBillingMap.COBB.serviceLocationMap.<key>.primaryRateScheduleId",
        "billing[].accountBillingMap.<key>.serviceBillingMap.ELEC.providerBillingMap.COBB.serviceLocationMap.<key>.revenueClass",
        "billing[].accountBillingMap.<key>.serviceBillingMap.ELEC.providerBillingMap.COBB.serviceLocationMap.<key>.serviceLocation",
        "billing[].accountBillingMap.<key>.serviceBillingMap.ELEC.providerBillingMap.COBB.serviceLocationMap.<key>.taxable",
        "billing[].accountBillingMap.<key>.serviceLocationMap.<key>.address.state",
        "billing[].accountBillingMap.<key>.serviceLocationMap.<key>.serviceLocation",
        "billing[].customerSummary.accounts[]",
        "billing[].customerSummary.users[]",
        "billing[].email",
        "login.expiration",
        "login.expiresIn",
        "login.isBusinessUser",
        "login.isSecondaryRegistration",
        "login.primaryUsername",
        "login.status",
        "login.username",
        "poll_daily.data.ELECTRIC[].accountNumber",
        "poll_daily.data.ELECTRIC[].baseSeries.data[].x",
        "poll_daily.data.ELECTRIC[].baseSeries.data[].y",
        "poll_daily.data.ELECTRIC[].baseUsageParameters.accountNumber",
        "poll_daily.data.ELECTRIC[].baseUsageParameters.endDateTime",
        "poll_daily.data.ELECTRIC[].baseUsageParameters.serviceLocationNumber",
        "poll_daily.data.ELECTRIC[].baseUsageParameters.startDateTime",
        "poll_daily.data.ELECTRIC[].baseUsageParameters.timeFrame",
        "poll_daily.data.ELECTRIC[].baseUsageParameters.userId",
        "poll_daily.data.ELECTRIC[].connectDate",
        "poll_daily.data.ELECTRIC[].endDateTime",
        "poll_daily.data.ELECTRIC[].meterToChartData.<key>[].x",
        "poll_daily.data.ELECTRIC[].meterToChartData.<key>[].y",
        "poll_daily.data.ELECTRIC[].meters[].accountNumber",
        "poll_daily.data.ELECTRIC[].meters[].meterNumber",
        "poll_daily.data.ELECTRIC[].meters[].rateName",
        "poll_daily.data.ELECTRIC[].meters[].seriesId",
        "poll_daily.data.ELECTRIC[].meters[].serviceLocationNumber",
        "poll_daily.data.ELECTRIC[].meters[].unitOfMeasure",
        "poll_daily.data.ELECTRIC[].series[].data[].x",
        "poll_daily.data.ELECTRIC[].series[].data[].y",
        "poll_daily.data.ELECTRIC[].series[].meterNumber",
        "poll_daily.data.ELECTRIC[].series[].name",
        "poll_daily.data.ELECTRIC[].series[].rateName",
        "poll_daily.data.ELECTRIC[].serviceLocationNumber",
        "poll_daily.data.ELECTRIC[].startDateTime",
        "poll_daily.data.ELECTRIC[].timeFrame",
        "poll_daily.data.ELECTRIC[].type",
        "poll_daily.data.ELECTRIC[].unitOfMeasure",
        "poll_daily.data.ELECTRIC[].xToOrderedInterval.<key>.interval.end",
        "poll_daily.data.ELECTRIC[].xToOrderedInterval.<key>.interval.start",
        "poll_daily.status",
        "poll_hourly.data.ELECTRIC[].accountNumber",
        "poll_hourly.data.ELECTRIC[].baseSeries.data[].x",
        "poll_hourly.data.ELECTRIC[].baseSeries.data[].y",
        "poll_hourly.data.ELECTRIC[].baseUsageParameters.accountNumber",
        "poll_hourly.data.ELECTRIC[].baseUsageParameters.endDateTime",
        "poll_hourly.data.ELECTRIC[].baseUsageParameters.serviceLocationNumber",
        "poll_hourly.data.ELECTRIC[].baseUsageParameters.startDateTime",
        "poll_hourly.data.ELECTRIC[].baseUsageParameters.timeFrame",
        "poll_hourly.data.ELECTRIC[].baseUsageParameters.userId",
        "poll_hourly.data.ELECTRIC[].connectDate",
        "poll_hourly.data.ELECTRIC[].endDateTime",
        "poll_hourly.data.ELECTRIC[].meterToChartData.<key>[].x",
        "poll_hourly.data.ELECTRIC[].meterToChartData.<key>[].y",
        "poll_hourly.data.ELECTRIC[].meters[].accountNumber",
        "poll_hourly.data.ELECTRIC[].meters[].meterNumber",
        "poll_hourly.data.ELECTRIC[].meters[].rateName",
        "poll_hourly.data.ELECTRIC[].meters[].seriesId",
        "poll_hourly.data.ELECTRIC[].meters[].serviceLocationNumber",
        "poll_hourly.data.ELECTRIC[].meters[].unitOfMeasure",
        "poll_hourly.data.ELECTRIC[].series[].data[].x",
        "poll_hourly.data.ELECTRIC[].series[].data[].y",
        "poll_hourly.data.ELECTRIC[].series[].meterNumber",
        "poll_hourly.data.ELECTRIC[].series[].name",
        "poll_hourly.data.ELECTRIC[].series[].rateName",
        "poll_hourly.data.ELECTRIC[].serviceLocationNumber",
        "poll_hourly.data.ELECTRIC[].startDateTime",
        "poll_hourly.data.ELECTRIC[].timeFrame",
        "poll_hourly.data.ELECTRIC[].type",
        "poll_hourly.data.ELECTRIC[].unitOfMeasure",
        "poll_hourly.data.ELECTRIC[].xToOrderedInterval.<key>.interval.end",
        "poll_hourly.data.ELECTRIC[].xToOrderedInterval.<key>.interval.start",
        "poll_hourly.status",
        "poll_pending.status",
        "service_location.id",
        "service_location.state",
        "user_data[].account",
        "user_data[].email",
        "user_data[].inactive",
        "user_data[].primaryServiceLocationId",
        "user_data[].serviceLocationIdToServiceLocationSummary.<key>.activeRateSchedules[]",
        "user_data[].serviceLocationIdToServiceLocationSummary.<key>.address.state",
        "user_data[].serviceLocationIdToServiceLocationSummary.<key>.id.serviceLocation",
        "user_data[].serviceLocationIdToServiceLocationSummary.<key>.id.srvLocNbr",
        "user_data[].serviceLocationIdToServiceLocationSummary.<key>.meterNumbersToExternalMeterBaseIds.<key>",
        "user_data[].serviceLocationIdToServiceLocationSummary.<key>.serviceStatus",
        "user_data[].serviceLocationToIndustries.<key>[]",
        "user_data[].serviceLocationToProviders.<key>[]",
        "user_data[].serviceLocationToUserDataServiceLocationSummaries.<key>[].activeRateSchedules[]",
        "user_data[].serviceLocationToUserDataServiceLocationSummaries.<key>[].address.state",
        "user_data[].serviceLocationToUserDataServiceLocationSummaries.<key>[].id.serviceLocation",
        "user_data[].serviceLocationToUserDataServiceLocationSummaries.<key>[].id.srvLocNbr",
        "user_data[].serviceLocationToUserDataServiceLocationSummaries.<key>[].serviceStatus",
        "user_data[].serviceLocationToUserDataServiceLocationSummaries.<key>[].services[]",
        "user_data[].serviceToProviders.ELEC[]",
        "user_data[].services[]",
    }
)  # fmt: skip

# Maps whose keys are data (account numbers, service location ids, meter
# numbers, interval timestamps, billing months, chart series descriptions)
# rather than field names. Their keys go through the value pass like any
# other string and are spelled `<key>` in a path.
IDENTIFIER_MAPS: Final = frozenset(
    {
        "accountBillingMap",
        "creditRatingHistory",
        "meterNumbersToExternalMeterBaseIds",
        "meterToChartData",
        "meters",
        "serviceLocationIdToServiceLocationSummary",
        "serviceLocationMap",
        "serviceLocationStatusMap",
        "serviceLocationToIndustries",
        "serviceLocationToProviders",
        "serviceLocationToUserDataServiceLocationSummaries",
        "xToOrderedInterval",
    }
)

# Values shorter than this are jurisdiction codes and status flags shared by
# every member of the co-op; the path pass is what scrubs those.
MIN_TOKEN_LENGTH: Final = 4

PLACEHOLDERS: Final[dict[type, Json]] = {
    str: "scrubbed",
    bool: False,
    int: 0,
    float: 0.0,
}


def read_credentials() -> dict[str, str]:
    """Read the portal credentials and identifiers `fnox exec` placed in the environment."""
    missing = sorted(
        variable
        for variable in CREDENTIAL_VARIABLES.values()
        if not os.environ.get(variable)
    )
    if missing:
        raise SystemExit(
            f"missing {', '.join(missing)}; run through `mise run fixtures:capture`"
        )
    credentials = {
        name: os.environ[variable]
        for name, variable in CREDENTIAL_VARIABLES.items()
        if name != "totp"
    }
    otp_uri = os.environ[CREDENTIAL_VARIABLES["totp"]]
    credentials["totp_secret"] = parse_qs(urlparse(otp_uri).query)["secret"][0]
    credentials["host"] = (
        credentials["host"].removeprefix("https://").removeprefix("http://").rstrip("/")
    )
    return credentials


class Tokens:
    """The real identifiers seen so far and the fake each one becomes."""

    def __init__(self) -> None:
        """Start with nothing harvested."""
        self._fakes: dict[str, str] = {}
        self._seen: set[str] = set()
        self._counts: dict[str, int] = dict.fromkeys(FAKES, 0)

    def add(self, kind: str, real: str | int | None) -> None:
        """Remember one identifier, numbering it after the others of its kind."""
        if real is None or real == "" or str(real) in self._seen:
            return
        self._seen.add(str(real))
        self._counts[kind] += 1
        n = self._counts[kind]
        fake = FIRST_FAKES.get(kind, "") if n == 1 else ""
        if not fake:
            fake = FAKES[kind].format(n=n)
        # A value already spelled like its fake is a fixture read back in;
        # tracking it would make the leak check flag the fake itself.
        if str(real) != fake:
            self._fakes[str(real)] = fake

    @property
    def fakes(self) -> dict[str, str]:
        """Return every harvested real value and its fake."""
        return dict(self._fakes)


def harvest(user_data: Json, into: Tokens) -> None:
    """Collect the identifiers of every account and location in `user-data`."""
    assert isinstance(user_data, list)
    for customer in user_data:
        assert isinstance(customer, dict)
        into.add("account", _text(customer.get("account")))
        into.add("email", _text(customer.get("email")))
        into.add("name", _text(customer.get("customerName")))
        into.add("address", _text(customer.get("address")))
        into.add("location", _text(customer.get("primaryServiceLocationId")))
        summaries = customer.get("serviceLocationToUserDataServiceLocationSummaries")
        by_id = customer.get("serviceLocationIdToServiceLocationSummary")
        for held in (summaries, by_id):
            if not isinstance(held, dict):
                continue
            for location_id, value in held.items():
                into.add("location", location_id)
                for summary in value if isinstance(value, list) else [value]:
                    _harvest_summary(summary, into)


def _harvest_summary(summary: Json, into: Tokens) -> None:
    if not isinstance(summary, dict):
        return
    ids = summary.get("id")
    if isinstance(ids, dict):
        into.add("location", _text(ids.get("serviceLocation")))
        into.add("location", _text(ids.get("srvLocNbr")))
    address = summary.get("address")
    if isinstance(address, dict):
        into.add("address", _text(address.get("addr1")))
    meters = summary.get("meterNumbersToExternalMeterBaseIds")
    if isinstance(meters, dict):
        for meter, base_id in meters.items():
            into.add("meter", meter)
            into.add("meter_base", _text(base_id))


def _text(value: Json) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str | int):
        return str(value)
    return None


def scrub_values(payload: Json, tokens: dict[str, str]) -> Json:
    """Replace every harvested identifier wherever it appears, keys included."""
    if isinstance(payload, dict):
        return {
            scrub_string(key, tokens): scrub_values(value, tokens)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [scrub_values(item, tokens) for item in payload]
    if isinstance(payload, str):
        return scrub_string(payload, tokens)
    # A service location id also arrives as a number (srvLocNbr), where string
    # replacement would never see it.
    if isinstance(payload, int) and not isinstance(payload, bool):
        fake = tokens.get(str(payload))
        if fake is not None and fake.isdigit():
            return int(fake)
    return payload


def scrub_string(value: str, tokens: dict[str, str]) -> str:
    """Replace every known real token inside one string, longest match first.

    Short tokens are skipped: several of these fields hold a single space, and
    substituting those would rewrite every series name in the poll responses.
    """
    for real in sorted(tokens, key=len, reverse=True):
        if len(real) >= MIN_TOKEN_LENGTH:
            value = value.replace(real, tokens[real])
    return value


def scrub_paths(payload: Json, path: str, parent: str, denied: set[str]) -> Json:
    """Keep allowed scalars, fake the faked ones, and placeholder the rest.

    The parent is the key a dict hangs under, which decides whether its keys
    are field names or data; a list resets it, since a list item hangs under
    no key.
    """
    if isinstance(payload, dict):
        return {
            key: scrub_paths(
                value,
                f"{path}.{'<key>' if parent in IDENTIFIER_MAPS else key}",
                key,
                denied,
            )
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [scrub_paths(item, f"{path}[]", "", denied) for item in payload]
    if payload is None or path in ALLOWED_PATHS:
        return payload
    if path in FAKED_PATHS:
        return FAKED_PATHS[path]
    denied.add(path)
    return PLACEHOLDERS[type(payload)]


def assert_clean(name: str, payload: Json, tokens: dict[str, str]) -> None:
    """Fail if any real value, or an auth token, survived the scrub."""
    serialized = json.dumps(payload)
    survivors = sorted(
        real for real in tokens if len(real) >= MIN_TOKEN_LENGTH and real in serialized
    )
    if survivors:
        places = sorted(
            {path for survivor in survivors for path in locate(payload, survivor)}
        )
        raise SystemExit(
            f"{name}: {len(survivors)} real value(s) survived scrubbing at "
            f"{places}; nothing was written"
        )
    if "authorizationToken" in serialized:
        raise SystemExit(f"{name}: an authorization token survived scrubbing")


def locate(payload: Json, needle: str, path: str = "") -> list[str]:
    """Return the JSON paths where a value survived, never the value itself."""
    if isinstance(payload, dict):
        return [
            found
            for key, value in payload.items()
            for found in (
                [f"{path}.<key>"]
                if needle in key
                else locate(value, needle, f"{path}.{key}")
            )
        ]
    if isinstance(payload, list):
        return [
            found
            for index, item in enumerate(payload)
            for found in locate(item, needle, f"{path}[{index}]")
        ]
    return [path] if needle in str(payload) else []


def scrub_all(raw: dict[str, Json], tokens: Tokens) -> dict[str, Json]:
    """Scrub every recorded response by value and then by path."""
    harvest(raw["user_data"], tokens)
    fakes = tokens.fakes
    denied: set[str] = set()
    scrubbed: dict[str, Json] = {}
    for name, payload in raw.items():
        by_value = scrub_values(payload, fakes)
        by_path = scrub_paths(by_value, name, "", denied)
        assert_clean(name, by_path, fakes)
        scrubbed[name] = by_path
    print(f"{len(denied)} path(s) recorded as placeholders:")
    for path in sorted(denied):
        print(f"  {path}")
    return scrubbed


def write_fixtures(scrubbed: dict[str, Json]) -> None:
    """Write every scrubbed response under tests/fixtures."""
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, payload in scrubbed.items():
        path = FIXTURES / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"wrote {path.relative_to(Path.cwd())}")


async def capture() -> None:
    """Pull every fixture, scrub it, and write it under tests/fixtures."""
    credentials = read_credentials()
    tokens = Tokens()
    tokens.add("account", credentials["account"])
    tokens.add("location", credentials["location"])
    tokens.add("meter", credentials["meter"])
    tokens.add("email", credentials["email"])
    raw: dict[str, Json] = {}

    async with aiohttp.ClientSession() as session:
        base = f"https://{credentials['host']}/services"

        async with session.post(
            f"{base}/oauth/auth/v2",
            data={
                "userId": credentials["email"],
                "password": credentials["password"],
                "twoFactorCode": pyotp.TOTP(credentials["totp_secret"]).now(),
            },
        ) as response:
            response.raise_for_status()
            login = await response.json()
        token = login.pop("authorizationToken")
        raw["login"] = login

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Nisc-Smarthub-Username": credentials["email"],
        }

        async with session.get(
            f"{base}/secured/user-data",
            headers=headers,
            params={"userId": credentials["email"]},
        ) as response:
            response.raise_for_status()
            raw["user_data"] = await response.json()

        async with session.get(
            f"{base}/secured/billing",
            headers=headers,
            params={
                "userId": credentials["email"],
                "accountNumber": credentials["account"],
            },
        ) as response:
            response.raise_for_status()
            raw["billing"] = await response.json()

        async with session.get(
            f"{base}/secured/service-locations/{credentials['location']}",
            headers=headers,
        ) as response:
            response.raise_for_status()
            raw["service_location"] = await response.json()

        for time_frame in ("HOURLY", "DAILY"):
            raw[f"poll_{time_frame.lower()}"] = await poll(
                session, base, headers, credentials, time_frame, raw
            )

    write_fixtures(scrub_all(raw, tokens))


def rescrub() -> None:
    """Run the scrub over the recorded fixtures and write them back."""
    raw: dict[str, Json] = {
        path.stem: json.loads(path.read_text())
        for path in sorted(FIXTURES.glob("*.json"))
    }
    write_fixtures(scrub_all(raw, Tokens()))


async def poll(
    session: aiohttp.ClientSession,
    base: str,
    headers: dict[str, str],
    credentials: dict[str, str],
    time_frame: str,
    raw: dict[str, Json],
) -> Json:
    """Run one usage poll to completion, recording the first PENDING answer."""
    body = {
        "timeFrame": time_frame,
        "userId": credentials["email"],
        "screen": "USAGE_EXPLORER",
        "includeDemand": False,
        "serviceLocationNumber": credentials["location"],
        "accountNumber": credentials["account"],
        "industries": ["ELECTRIC"],
        "startDateTime": str(int(WINDOW_START.timestamp()) * 1000),
        "endDateTime": str(int(WINDOW_END.timestamp()) * 1000),
    }
    for attempt in range(1, 7):
        async with session.post(
            f"{base}/secured/utility-usage/poll", headers=headers, json=body
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        status = payload["status"]
        print(f"{time_frame} poll attempt {attempt}: {status}")
        if status == "COMPLETE":
            return payload
        if status == "PENDING":
            raw.setdefault("poll_pending", payload)
            await asyncio.sleep(4)
            continue
        raise SystemExit(f"{time_frame} poll answered with status {status}")
    raise SystemExit(f"{time_frame} poll never completed")


if __name__ == "__main__":
    if sys.argv[1:] == ["--from-fixtures"]:
        rescrub()
    else:
        asyncio.run(capture())
