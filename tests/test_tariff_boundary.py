"""NHEC's flat tariff, shaped unlike NiteFlex, through every shared surface.

It prices from usage with no periods, has no allowance, reads different lines
off a bill, and lets the version in effect on the day a cycle's bill is
rendered govern the whole cycle. Nothing outside its own module names it,
which is what keeps a new rate schedule local.
"""

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.components.recorder.statistics import list_statistic_ids
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntry,
    ConfigEntryState,
)
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.recorder import get_instance
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.nisc_smarthub.const import (
    CONF_RATE_VERSIONS,
    CONF_TARIFF,
    DOMAIN,
    SERVICE_FINALIZE_CYCLE,
)
from custom_components.nisc_smarthub.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.nisc_smarthub.smarthub import ClientError
from custom_components.nisc_smarthub.tariff import (
    NHEC_RESIDENTIAL,
    NITEFLEX,
    TARIFFS,
    RateVersion,
    rate_version_to_data,
    tariff_for,
)

from . import recordings
from .test_config_flow import CREDENTIALS
from .test_init import statistic_sums

pytestmark = pytest.mark.usefixtures("recorder_mock")

HOST = "nhec.smarthub.coop"
FEBRUARY, AUGUST = (
    rate_version_to_data(one) for one in NHEC_RESIDENTIAL.published_versions
)

# The recorded 157.55 kWh, and 72 of the cycle's 744 hours of the $29 member
# charge, 2.80645, at each version's per-kWh total.
AT_FEBRUARY = 157.55 * 0.22053 + 29 * 72 / 744
AT_AUGUST = 157.55 * 0.24713 + 29 * 72 / 744


@pytest.fixture(autouse=True)
async def portal_timezone(hass: HomeAssistant) -> None:
    """Run these tests in the portal's zone, as the instance is configured."""
    await hass.config.async_set_time_zone(str(recordings.PORTAL_ZONE))


@pytest.fixture
def nhec_portal(mock_client: AsyncMock) -> AsyncMock:
    """Serve the recorded hours from NHEC's portal, which classifies nothing."""
    customers = recordings.customers(recordings.LOCATION)
    location = customers[0].locations[0]
    mock_client.user_data.return_value = [
        replace(
            customers[0],
            locations=(replace(location, active_rate_schedules=("B:COOP",)),),
        )
    ]
    mock_client.poll_hourly.return_value = recordings.poll_result(
        usage=recordings.hourly_usage(), periods={}
    )
    return mock_client


async def open_rates(hass: HomeAssistant) -> Any:
    """Walk the setup flow on NHEC's portal to the rates step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**CREDENTIALS, CONF_HOST: HOST}
    )
    assert result["step_id"] == "cycle"
    return result


async def configure(
    hass: HomeAssistant,
    first: dict[str, Any] = FEBRUARY,
    later: list[dict[str, Any]] | None = None,
) -> ConfigEntry:
    """Set an NHEC entry up on one version, then store any later ones."""
    result = await open_rates(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "cycle_day": 28,
            **{f"nhec_residential_{key}": value for key, value in first.items()},
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry: ConfigEntry = result["result"]
    if later:
        hass.config_entries.async_update_entry(
            entry, data=entry.data | {CONF_RATE_VERSIONS: [first, *later]}
        )
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    return entry


def seeded(result: Any) -> dict[str, Any]:
    """Return the rate fields a form is pre-filled with."""
    return {
        str(key.schema).removeprefix("nhec_residential_"): key.default()
        for key in result["data_schema"].schema
        if str(key.schema).startswith("nhec_residential_")
    }


async def test_setup_offers_only_the_matched_tariffs_fields(
    hass: HomeAssistant, nhec_portal: AsyncMock
) -> None:
    """The form is NHEC's, and nothing of NiteFlex's survives in it."""
    result = await open_rates(hass)

    assert result["description_placeholders"] == {"tariff": "NHEC Residential (B)"}
    assert sorted(str(key.schema) for key in result["data_schema"].schema) == [
        "cycle_day",
        "nhec_residential_co_op_power_rate",
        "nhec_residential_delivery_rate",
        "nhec_residential_effective_from",
        "nhec_residential_regional_access_rate",
        "nhec_residential_service_charge",
        "nhec_residential_system_benefits_rate",
    ]


@pytest.mark.parametrize(
    ("connect_date", "co_op_power_rate"),
    [
        pytest.param(datetime(2026, 3, 21, 4, tzinfo=UTC), 0.11117, id="spring"),
        pytest.param(recordings.CONNECT_DATE, 0.13968, id="after_august"),
    ],
)
async def test_setup_seeds_the_rates_in_effect_at_the_connect_date(
    hass: HomeAssistant,
    nhec_portal: AsyncMock,
    connect_date: datetime,
    co_op_power_rate: float,
) -> None:
    """A spring member's first cycle predates August's rates, which would be refused."""
    nhec_portal.billing.return_value = replace(
        recordings.billing_summary(), connect_date=connect_date
    )

    result = await open_rates(hass)

    assert seeded(result)["co_op_power_rate"] == co_op_power_rate


async def test_a_portal_that_will_not_say_its_connect_date_seeds_the_newest(
    hass: HomeAssistant, nhec_portal: AsyncMock
) -> None:
    """The seed is a convenience; the submission is what reports the failure."""
    nhec_portal.billing.side_effect = ClientError("no route")

    result = await open_rates(hass)

    assert seeded(result)["effective_from"] == "2026-08-01"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"cycle_day": 28, **{f"nhec_residential_{k}": v for k, v in AUGUST.items()}},
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_an_nhec_entry_prices_usage_with_no_classification(
    hass: HomeAssistant,
    nhec_portal: AsyncMock,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Usage and cost are written, and no hour is an unclassified hole."""
    entry = await configure(hass, AUGUST)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_TARIFF] == "nhec_residential"
    written = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, None, None
    )
    assert sorted(one["statistic_id"] for one in written) == [
        recordings.COST_STATISTIC_ID,
        recordings.USAGE_STATISTIC_ID,
    ]
    assert nhec_portal.poll_hourly.call_args.kwargs == {"time_of_use": False}
    sums = await statistic_sums(
        hass, recordings.USAGE_STATISTIC_ID, recordings.COST_STATISTIC_ID
    )
    assert sums[recordings.USAGE_STATISTIC_ID] == pytest.approx(157.55)
    assert sums[recordings.COST_STATISTIC_ID] == pytest.approx(AT_AUGUST, abs=1e-5)
    assert [one.issue_id for one in issue_registry.issues.values()] == []
    keys = [
        one.unique_id
        for one in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    ]
    assert not any("allowance" in key for key in keys)


async def test_an_entry_on_older_rates_is_offered_the_newer_ones(
    hass: HomeAssistant, nhec_portal: AsyncMock, issue_registry: ir.IssueRegistry
) -> None:
    """The repair names August, and adding a version is seeded with it."""
    entry = await configure(hass, FEBRUARY)

    issue = issue_registry.async_get_issue(
        DOMAIN, f"newer_rate_version_{entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders == {
        "effective_from": "2026-08-01",
        "tariff": "NHEC Residential (B)",
    }
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "add_rate_version"}
    )
    assert seeded(result) == AUGUST


@pytest.mark.parametrize(
    ("effective_from", "expected"),
    [
        pytest.param("2026-09-15", AT_AUGUST, id="before_the_bill"),
        pytest.param("2026-09-28", AT_AUGUST, id="on_the_bill_day"),
        pytest.param("2026-09-29", AT_FEBRUARY, id="after_the_bill"),
    ],
)
async def test_the_version_on_the_bill_day_governs_the_whole_cycle(
    hass: HomeAssistant, nhec_portal: AsyncMock, effective_from: str, expected: float
) -> None:
    """Every recorded hour precedes the version, and it still prices them."""
    entry = await configure(
        hass, FEBRUARY, [{**AUGUST, "effective_from": effective_from}]
    )

    sums = await statistic_sums(hass, recordings.COST_STATISTIC_ID)
    assert entry.state is ConfigEntryState.LOADED
    assert sums[recordings.COST_STATISTIC_ID] == pytest.approx(expected, abs=1e-5)


async def test_a_first_version_may_date_after_the_connect_date(
    hass: HomeAssistant, nhec_portal: AsyncMock
) -> None:
    """The first cycle's bill is what the first version has to be in effect for."""
    entry = await configure(hass, {**AUGUST, "effective_from": "2026-09-15"})

    assert entry.state is ConfigEntryState.LOADED
    sums = await statistic_sums(hass, recordings.COST_STATISTIC_ID)
    assert sums[recordings.COST_STATISTIC_ID] == pytest.approx(AT_AUGUST, abs=1e-5)


async def test_a_first_version_after_the_first_bill_is_refused(
    hass: HomeAssistant, nhec_portal: AsyncMock
) -> None:
    """Nothing would price the first cycle, so the form says so."""
    result = await open_rates(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "cycle_day": 28,
            **{f"nhec_residential_{k}": v for k, v in AUGUST.items()},
            "nhec_residential_effective_from": "2026-09-29",
        },
    )

    assert result["errors"] == {
        "nhec_residential_effective_from": "rate_version_misses_first_cycle"
    }


def finalize_call(entry: ConfigEntry, **lines: float) -> dict[str, Any]:
    """Return a finalize call for the cycle holding the recorded hours."""
    return {
        "config_entry_id": entry.entry_id,
        "cycle_start": "2026-08-28",
        "read_start": "2026-08-28",
        "read_end": "2026-09-04",
        "bill_total": 40.0,
        **lines,
    }


async def test_an_nhec_bill_is_finalized_with_its_own_lines(
    hass: HomeAssistant, nhec_portal: AsyncMock
) -> None:
    """The bill's lines are the tariff's, and the cycle adds up to the bill."""
    entry = await configure(hass, FEBRUARY)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_FINALIZE_CYCLE,
        finalize_call(entry, service_charge=25.0),
        blocking=True,
    )
    await async_wait_recording_done(hass)

    sums = await statistic_sums(hass, recordings.COST_STATISTIC_ID)
    assert sums[recordings.COST_STATISTIC_ID] == pytest.approx(40.0, abs=1e-6)
    # The bill's $25, not the schedule's $29, over 72 of the 168 hours to the
    # read end: 157.55 * 0.22053 + 25 * 72 / 168 = 45.45879, and 40 - 45.45879.
    records = (await async_get_config_entry_diagnostics(hass, entry))["cycle_records"]
    assert records["2026-08-28"]["values"] == {"service_charge": 25.0}
    assert records["2026-08-28"]["residual"] == pytest.approx(-5.45879, abs=1e-5)


async def test_an_nhec_bill_refuses_niteflex_lines(
    hass: HomeAssistant, nhec_portal: AsyncMock
) -> None:
    """A line this tariff does not read is a mistake, not something to ignore."""
    entry = await configure(hass, FEBRUARY)

    with pytest.raises(ServiceValidationError) as refused:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_FINALIZE_CYCLE,
            finalize_call(entry, service_charge=29.0, pca_factor=0.01),
            blocking=True,
        )

    assert refused.value.translation_key == "bill_line_unexpected"
    assert refused.value.translation_placeholders == {
        "field": "pca_factor",
        "tariff": "NHEC Residential (B)",
    }
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["cycle_records"] == {}


def test_a_usage_tariff_cannot_name_periods() -> None:
    """Periods only mean something to a tariff priced from time of use."""
    with pytest.raises(ValueError, match="names periods"):
        replace(NHEC_RESIDENTIAL, periods=NITEFLEX.periods)


def test_a_flat_tariff_sums_only_declared_rates() -> None:
    """A per-kWh line has to be a rate the form asks for."""
    with pytest.raises(ValueError, match="undeclared"):
        replace(NHEC_RESIDENTIAL, energy_rates=("demand_rate",))


def test_two_tariffs_matching_one_location_is_an_error() -> None:
    """Registry order never decides which rate schedule a member is billed under."""
    twin = replace(NHEC_RESIDENTIAL, key="twin")
    with (
        patch.dict(TARIFFS, {twin.key: twin}),
        pytest.raises(ValueError, match="all match"),
    ):
        tariff_for(HOST, ["B:COOP"])


def test_a_published_version_has_to_conform() -> None:
    """A shipped default is held to the same rules as a typed one."""
    broken = RateVersion(
        effective_from=date(2026, 8, 1), values={"co_op_power_rate": 0.1}
    )
    with pytest.raises(ValueError, match="missing"):
        replace(NHEC_RESIDENTIAL, published_versions=(broken,))
