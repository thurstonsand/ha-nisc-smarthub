"""The user config flow, from credentials to a configured service location."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_LOCATION, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
import voluptuous as vol

from custom_components.nisc_smarthub.const import (
    CONF_ACCOUNT,
    CONF_CYCLE_DAY,
    CONF_POLL_INTERVAL_MINUTES,
    CONF_RATE_VERSIONS,
    CONF_TARIFF,
    CONF_TOTP_SECRET,
    DEFAULT_POLL_INTERVAL_MINUTES,
    DOMAIN,
)
from custom_components.nisc_smarthub.smarthub import (
    AuthError,
    ClientError,
    MultipleMeters,
    MultipleProviders,
    PollTimeout,
)
from custom_components.nisc_smarthub.tariff import (
    NITEFLEX,
    TARIFFS,
    rate_version_to_data,
)

from . import recordings

# The integration depends on the recorder, so the flow cannot even load the
# integration without one.
pytestmark = pytest.mark.usefixtures("recorder_mock", "mock_client")

PUBLISHED = rate_version_to_data(NITEFLEX.newest_published_version())


def cycle_input(**overrides: object) -> dict[str, object]:
    """Return the tariff step's input, published rates and all."""
    return {CONF_TARIFF: NITEFLEX.key, CONF_CYCLE_DAY: 28, **PUBLISHED, **overrides}


CREDENTIALS = {
    CONF_HOST: recordings.HOST,
    CONF_EMAIL: recordings.EMAIL,
    CONF_PASSWORD: recordings.PASSWORD,
    CONF_TOTP_SECRET: recordings.TOTP_SECRET,
}


@pytest.fixture(autouse=True)
async def portal_timezone(hass: HomeAssistant) -> None:
    """Run these tests in the portal's zone, as the instance is configured."""
    await hass.config.async_set_time_zone(str(recordings.PORTAL_ZONE))


async def start(hass: HomeAssistant) -> ConfigFlowResult:
    """Open the user flow and hand it the credentials."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )


async def test_one_service_location_skips_the_choice(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """A login with a single premise goes straight to the billing cycle."""
    result = await start(hass)
    assert result["step_id"] == "cycle"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], cycle_input()
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == recordings.DESCRIPTION
    assert result["data"] == {
        **CREDENTIALS,
        CONF_ACCOUNT: recordings.ACCOUNT,
        CONF_LOCATION: recordings.LOCATION,
        CONF_CYCLE_DAY: 28,
        CONF_TARIFF: NITEFLEX.key,
        CONF_RATE_VERSIONS: [PUBLISHED],
    }
    assert result["result"].unique_id == f"{recordings.ACCOUNT}_{recordings.LOCATION}"

    # The first poll is the flow's verification; the entry's own setup polls
    # again straight after.
    _, _, start_bound, end_bound = mock_client.poll_hourly.call_args_list[0].args
    assert end_bound - start_bound == timedelta(days=3)


async def test_a_host_typed_as_a_url_is_stored_as_a_host(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """The form calls the field a URL; the entry keeps the bare host."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**CREDENTIALS, CONF_HOST: f"https://{recordings.HOST}/"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], cycle_input()
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == recordings.HOST


async def test_two_service_locations_are_offered(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """More than one premise means the user picks which one this entry covers."""
    mock_client.user_data.return_value = recordings.customers(
        recordings.LOCATION, recordings.SECOND_LOCATION
    )

    result = await start(hass)
    assert result["step_id"] == "location"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_LOCATION: f"{recordings.ACCOUNT}:{recordings.SECOND_LOCATION}"},
    )
    assert result["step_id"] == "cycle"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], cycle_input(cycle_day=1)
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_LOCATION] == recordings.SECOND_LOCATION
    assert (
        result["result"].unique_id
        == f"{recordings.ACCOUNT}_{recordings.SECOND_LOCATION}"
    )


async def test_a_configured_location_is_not_configured_twice(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The same service location aborts on its unique id."""
    mock_config_entry.add_to_hass(hass)

    result = await start(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_a_login_with_no_locations_aborts(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """A login with nothing to track has nothing to configure."""
    mock_client.user_data.return_value = recordings.customers()

    result = await start(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_service_locations"


async def test_a_second_provider_aborts(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """An account the client will not read is not worth an entry."""
    mock_client.billing.side_effect = MultipleProviders("two providers")

    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], cycle_input(cycle_day=1)
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "multiple_providers"


async def test_a_second_meter_aborts(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """A location with two registers is refused by name, not as a provider."""
    mock_client.poll_hourly.side_effect = MultipleMeters("two meters")

    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], cycle_input(cycle_day=1)
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "multiple_meters"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(AuthError("rejected"), "invalid_auth", id="invalid_auth"),
        pytest.param(ClientError("no route"), "cannot_connect", id="cannot_connect"),
    ],
)
async def test_the_credentials_step_recovers(
    hass: HomeAssistant, mock_client: AsyncMock, error: Exception, expected: str
) -> None:
    """A failed login shows the error, and the next attempt still works."""
    mock_client.login.side_effect = error

    result = await start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    mock_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    assert result["step_id"] == "cycle"


async def test_an_expired_token_at_the_last_step_recovers(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """The portal's token outlives five minutes, and the flow may not."""
    result = await start(hass)
    mock_client.login.side_effect = AuthError("rejected the token")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], cycle_input(cycle_day=1)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    mock_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], cycle_input(cycle_day=1)
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_a_poll_that_never_completes_blocks_the_entry(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """No entry is created until the portal has answered a real poll."""
    mock_client.poll_hourly.side_effect = PollTimeout("still pending")

    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], cycle_input(cycle_day=1)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    mock_client.poll_hourly.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], cycle_input(cycle_day=1)
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


def defaults(schema: vol.Schema | None) -> dict[str, object]:
    """Return each field's default in a shown form."""
    assert schema is not None
    return {
        str(key.schema): cast("Callable[[], object]", key.default)()
        for key in schema.schema
        if isinstance(key, vol.Required) and key.default is not vol.UNDEFINED
    }


async def test_the_tariff_step_seeds_the_published_rates(hass: HomeAssistant) -> None:
    """The rate schedule the location is on arrives already filled in."""
    result = await start(hass)
    assert result["step_id"] == "cycle"

    schema = result["data_schema"]
    seeded = defaults(schema)
    assert seeded[CONF_TARIFF] == NITEFLEX.key
    assert seeded["effective_from"] == "2026-01-01"
    assert seeded["on_peak_rate"] == 0.14
    assert seeded["off_peak_rate"] == 0.075
    assert seeded["super_off_peak_rate"] == 0.05
    assert seeded["super_off_peak_allowance"] == 400.0
    assert seeded["service_charge"] == 33.0
    assert seeded["sales_tax_rate"] == 0.0775

    assert schema is not None
    options = schema.schema[vol.Required(CONF_TARIFF)].config["options"]
    assert [one["value"] for one in options] == list(TARIFFS)


async def test_edited_rates_are_what_the_entry_stores(hass: HomeAssistant) -> None:
    """The seed is a suggestion; what the user submits is the rate version."""
    result = await start(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        cycle_input(on_peak_rate=0.155, effective_from="2026-08-01"),
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    version = result["data"][CONF_RATE_VERSIONS][0]
    assert version["on_peak_rate"] == 0.155
    assert version["effective_from"] == "2026-08-01"
    assert result["data"][CONF_TARIFF] == NITEFLEX.key
    assert result["result"].state is ConfigEntryState.LOADED


async def test_a_first_version_dated_after_the_connect_date_is_refused(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """Hours from the connect date on need a rate, and this version has none for them."""
    result = await start(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], cycle_input(effective_from="2026-08-29")
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"effective_from": "rate_version_after_connect_date"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], cycle_input(effective_from="2026-08-28")
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        pytest.param("on_peak_rate", -0.14, "negative_number", id="negative_rate"),
        pytest.param("off_peak_rate", float("nan"), "not_a_number", id="nan_rate"),
        pytest.param(
            "super_off_peak_rate", float("inf"), "not_a_number", id="infinite_rate"
        ),
        pytest.param(
            "super_off_peak_allowance", -1.0, "negative_number", id="negative_allowance"
        ),
        pytest.param("service_charge", -33.0, "negative_number", id="negative_charge"),
        pytest.param("sales_tax_rate", -0.01, "negative_number", id="negative_tax"),
        pytest.param("pca_factor", float("nan"), "not_a_number", id="nan_rider"),
        pytest.param(
            "recurring_adjustment",
            float("-inf"),
            "not_a_number",
            id="infinite_adjustment",
        ),
    ],
)
async def test_a_rate_field_that_is_not_a_price_is_refused(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    field: str,
    value: float,
    reason: str,
) -> None:
    """Every number has to be finite, and every quantity zero or more."""
    result = await start(hass)
    polls = mock_client.poll_hourly.call_count

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], cycle_input(**{field: value})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {field: reason}
    assert mock_client.poll_hourly.call_count == polls


async def test_a_fractional_cycle_day_is_refused(hass: HomeAssistant) -> None:
    """The selector's own bounds stop 0 and 32; a half day gets through to us."""
    result = await start(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], cycle_input(cycle_day=28.5)
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CYCLE_DAY: "invalid_cycle_day"}


async def test_a_rejected_form_keeps_what_was_typed(hass: HomeAssistant) -> None:
    """The user fixes one field, not the whole form."""
    result = await start(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], cycle_input(on_peak_rate=-1.0, service_charge=35.0)
    )

    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"]
    assert schema is not None
    suggested = {
        str(key.schema): key.description["suggested_value"]
        for key in schema.schema
        if key.description and "suggested_value" in key.description
    }
    assert suggested["service_charge"] == 35.0
    assert suggested["on_peak_rate"] == -1.0


async def test_a_location_on_an_unknown_rate_schedule_aborts(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """No tariff prices it, so there is nothing to configure yet."""
    customers = recordings.customers(recordings.LOCATION)
    location = customers[0].locations[0]
    mock_client.user_data.return_value = [
        replace(
            customers[0],
            locations=(replace(location, active_rate_schedules=("RES:COBB",)),),
        )
    ]

    result = await start(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_matching_tariff"
    assert result["description_placeholders"] == {"codes": "RES:COBB"}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(AuthError("rejected"), "invalid_auth", id="invalid_auth"),
        pytest.param(ClientError("no route"), "cannot_connect", id="cannot_connect"),
    ],
)
async def test_reauth_replaces_the_credentials_the_portal_rejected(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    error: Exception,
    expected: str,
) -> None:
    """A rejected attempt shows the error, and the next one is stored."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders[CONF_EMAIL] == recordings.EMAIL

    mock_client.login.side_effect = error
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PASSWORD: "wrong", CONF_TOTP_SECRET: recordings.TOTP_SECRET},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    mock_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PASSWORD: "hunter3", CONF_TOTP_SECRET: "NEWSECRET"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "hunter3"
    assert mock_config_entry.data[CONF_TOTP_SECRET] == "NEWSECRET"
    assert mock_config_entry.data[CONF_EMAIL] == recordings.EMAIL


async def reconfigure(
    hass: HomeAssistant, entry: MockConfigEntry, option: str
) -> ConfigFlowResult:
    """Open the reconfigure menu and pick one of its items."""
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reconfigure"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": option}
    )


async def test_a_rate_version_is_added_to_the_configured_ones(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The form is seeded from the newest version, dated today."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await reconfigure(hass, mock_config_entry, "add_rate_version")
    assert result["step_id"] == "add_rate_version"
    seeded = defaults(result["data_schema"])
    assert seeded["effective_from"] == "2026-09-11"
    assert seeded["on_peak_rate"] == PUBLISHED["on_peak_rate"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**PUBLISHED, "effective_from": "2026-09-01", "on_peak_rate": 0.2},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    versions = mock_config_entry.data[CONF_RATE_VERSIONS]
    assert [one["effective_from"] for one in versions] == ["2026-01-01", "2026-09-01"]
    assert versions[1]["on_peak_rate"] == 0.2
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_a_rate_version_dated_like_another_is_refused(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Two versions on one day have no order to price under."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await reconfigure(hass, mock_config_entry, "add_rate_version")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**PUBLISHED, "effective_from": "2026-01-01"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"effective_from": "duplicate_rate_version"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**PUBLISHED, "effective_from": "2026-02-01"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT


async def test_the_newest_rate_version_is_removed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Removing it puts the hours it governed back under the one before it."""
    older = {**PUBLISHED, "effective_from": "2025-01-01"}
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data=mock_config_entry.data | {CONF_RATE_VERSIONS: [older, PUBLISHED]},
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await reconfigure(hass, mock_config_entry, "remove_rate_version")
    assert result["step_id"] == "remove_rate_version"
    assert result["description_placeholders"] == {"effective_from": "2026-01-01"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert mock_config_entry.data[CONF_RATE_VERSIONS] == [older]


async def test_the_last_rate_version_is_not_removed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Without a version every hour of history is unpriced."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await reconfigure(hass, mock_config_entry, "remove_rate_version")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "last_rate_version"
    assert len(mock_config_entry.data[CONF_RATE_VERSIONS]) == 1


async def test_a_new_cycle_day_reloads_the_entry_and_resplits_its_cycles(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The window is rebuilt around the new boundary on the reload."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _bounds(mock_config_entry) == [
        (recordings.CONNECT_DATE, datetime(2026, 9, 28, 4, tzinfo=UTC))
    ]

    result = await reconfigure(hass, mock_config_entry, "cycle_day")
    assert result["step_id"] == "cycle_day"
    assert defaults(result["data_schema"]) == {CONF_CYCLE_DAY: 28}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CYCLE_DAY: 15.5}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CYCLE_DAY: "invalid_cycle_day"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CYCLE_DAY: 15}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert mock_config_entry.data[CONF_CYCLE_DAY] == 15
    assert _bounds(mock_config_entry) == [
        (recordings.CONNECT_DATE, datetime(2026, 9, 15, 4, tzinfo=UTC))
    ]


def _bounds(entry: MockConfigEntry) -> list[tuple[datetime, datetime]]:
    plan = entry.runtime_data.last_plan
    assert plan is not None
    return [(one.start, one.end) for one in plan.cycles]


async def test_new_credentials_have_to_reach_the_same_location(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """A login for someone else's premise would rewrite this entry's history."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await reconfigure(hass, mock_config_entry, "credentials")
    assert result["step_id"] == "credentials"

    mock_client.user_data.return_value = recordings.customers(
        recordings.SECOND_LOCATION
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: "other@example.com",
            CONF_PASSWORD: "hunter3",
            CONF_TOTP_SECRET: "NEWSECRET",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_account"}
    assert mock_config_entry.data[CONF_EMAIL] == recordings.EMAIL

    mock_client.user_data.return_value = recordings.customers(recordings.LOCATION)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: "other@example.com",
            CONF_PASSWORD: "hunter3",
            CONF_TOTP_SECRET: "NEWSECRET",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_EMAIL] == "other@example.com"
    assert mock_config_entry.data[CONF_PASSWORD] == "hunter3"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(AuthError("rejected"), "invalid_auth", id="invalid_auth"),
        pytest.param(ClientError("no route"), "cannot_connect", id="cannot_connect"),
    ],
)
async def test_the_credentials_step_reports_what_the_portal_said(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    error: Exception,
    expected: str,
) -> None:
    """A rejected login leaves the stored one alone."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await reconfigure(hass, mock_config_entry, "credentials")
    mock_client.login.side_effect = error
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: recordings.EMAIL,
            CONF_PASSWORD: "hunter3",
            CONF_TOTP_SECRET: recordings.TOTP_SECRET,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    assert mock_config_entry.data[CONF_PASSWORD] == recordings.PASSWORD


async def test_the_poll_interval_is_tuned_through_the_options(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """The entry reloads onto the new interval rather than finishing the old one."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["step_id"] == "init"
    assert defaults(result["data_schema"]) == {
        CONF_POLL_INTERVAL_MINUTES: DEFAULT_POLL_INTERVAL_MINUTES
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL_MINUTES: 45.5}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_POLL_INTERVAL_MINUTES: "invalid_poll_interval"}
    assert mock_config_entry.options == {}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL_MINUTES: 45}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {CONF_POLL_INTERVAL_MINUTES: 45}
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_client.poll_hourly.call_count == 2

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=45))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_client.poll_hourly.call_count == 3
