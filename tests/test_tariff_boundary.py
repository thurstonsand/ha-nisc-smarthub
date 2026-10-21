"""A second tariff, shaped unlike NiteFlex, through every shared surface.

The tariff below exists only here. It prices from usage with no periods, has
no allowance, reads different lines off a bill, and lets the version in effect
on the day a cycle ends govern the whole cycle. Nothing outside its own
definition changes for it, which is what keeps a new rate schedule local.
"""

from collections.abc import Generator, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.components.recorder.statistics import list_statistic_ids
from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigEntryState
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
from custom_components.nisc_smarthub.tariff import (
    NITEFLEX,
    TARIFFS,
    Cycle,
    CycleActuals,
    HourCost,
    HourUsage,
    Parameter,
    PricedCycle,
    RateVersion,
    Tariff,
    TariffSource,
    nominal_hours,
    tariff_for,
    version_for,
)

from . import recordings
from .test_config_flow import CREDENTIALS
from .test_init import statistic_sums

pytestmark = pytest.mark.usefixtures("recorder_mock")

HOST = "flat.smarthub.coop"
RECORDED_KWH = 157.55


@dataclass(frozen=True, kw_only=True)
class BillDateFlatTariff(Tariff):
    """One price per kWh and a member charge, at the rates of the bill's day."""

    def governs_cycle(self, version: RateVersion, cycle: Cycle) -> bool:
        """Say whether the version is in effect on the day the cycle's bill is."""
        return version.effective_from <= cycle.end.date()

    def price_cycle(
        self,
        cycle: Cycle,
        hours: Sequence[HourUsage],
        versions: Sequence[RateVersion],
        actuals: CycleActuals | None,
    ) -> PricedCycle:
        """Price every hour of the cycle under the version in effect when it ends."""
        bill_day = (cycle.end if actuals is None else actuals.read_end).date()
        version = version_for(versions, bill_day)
        assert version is not None
        rates = version.values
        bill = rates if actuals is None else actuals.values
        member = bill["member_charge"]
        per_kwh = rates["delivery_rate"] + rates["power_rate"]
        hourly = nominal_hours(cycle)
        return PricedCycle(
            costs=[
                HourCost(
                    start=hour.start,
                    energy=hour.total * per_kwh,
                    fixed=member / hourly,
                    rider=0.0,
                    tax=0.0,
                    adjustment=0.0,
                    residual=0.0,
                )
                for hour in sorted(hours, key=lambda one: one.start)
            ],
            allowance_remaining={},
        )


FLAT = BillDateFlatTariff(
    key="flat",
    display_name="Example Co-op flat rate",
    source=TariffSource.USAGE,
    periods=(),
    allowances=(),
    portal_hosts=(HOST,),
    rate_schedule_codes=("B:FLAT",),
    rate_parameters=(
        Parameter(key="delivery_rate", signed=False),
        Parameter(key="power_rate", signed=False),
        Parameter(key="member_charge", signed=False),
    ),
    bill_parameters=(Parameter(key="member_charge", signed=False),),
    published_versions=(
        RateVersion(
            effective_from=date(2026, 1, 1),
            values={"delivery_rate": 0.05, "power_rate": 0.10, "member_charge": 30.0},
        ),
    ),
)

PUBLISHED = {
    "effective_from": "2026-01-01",
    "delivery_rate": 0.05,
    "power_rate": 0.10,
    "member_charge": 30.0,
}


@pytest.fixture(autouse=True)
def registered() -> Generator[None]:
    """Ship the flat tariff alongside NiteFlex for the length of a test."""
    with patch.dict(TARIFFS, {FLAT.key: FLAT}):
        yield


@pytest.fixture(autouse=True)
async def portal_timezone(hass: HomeAssistant) -> None:
    """Run these tests in the portal's zone, as the instance is configured."""
    await hass.config.async_set_time_zone(str(recordings.PORTAL_ZONE))


@pytest.fixture
def flat_portal(mock_client: AsyncMock) -> AsyncMock:
    """Serve the recorded hours from a flat-rate portal that classifies nothing."""
    customers = recordings.customers(recordings.LOCATION)
    location = customers[0].locations[0]
    mock_client.user_data.return_value = [
        replace(
            customers[0],
            locations=(replace(location, active_rate_schedules=("B:FLAT",)),),
        )
    ]
    mock_client.poll_hourly.return_value = recordings.poll_result(
        usage=recordings.hourly_usage(), periods={}
    )
    return mock_client


async def configure(
    hass: HomeAssistant,
    versions: list[dict[str, Any]],
    effective_from: str = "2026-01-01",
) -> ConfigEntry:
    """Walk the setup flow on the flat portal, then store any later versions."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**CREDENTIALS, CONF_HOST: HOST}
    )
    assert result["step_id"] == "cycle"
    assert result["description_placeholders"] == {"tariff": FLAT.display_name}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "cycle_day": 28,
            **{f"flat_{key}": value for key, value in PUBLISHED.items()},
            "flat_effective_from": effective_from,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry: ConfigEntry = result["result"]
    if versions:
        hass.config_entries.async_update_entry(
            entry, data=entry.data | {CONF_RATE_VERSIONS: [PUBLISHED, *versions]}
        )
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    return entry


async def test_setup_offers_only_the_matched_tariffs_fields(
    hass: HomeAssistant, flat_portal: AsyncMock
) -> None:
    """The form is the flat tariff's, and nothing of NiteFlex's survives in it."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**CREDENTIALS, CONF_HOST: HOST}
    )

    schema = result["data_schema"]
    assert schema is not None
    assert sorted(str(key.schema) for key in schema.schema) == [
        "cycle_day",
        "flat_delivery_rate",
        "flat_effective_from",
        "flat_member_charge",
        "flat_power_rate",
    ]


async def test_a_flat_entry_prices_usage_with_no_classification(
    hass: HomeAssistant,
    flat_portal: AsyncMock,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Usage and cost are written, and no hour is an unclassified hole."""
    entry = await configure(hass, [])

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_TARIFF] == "flat"
    written = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, None, None
    )
    assert sorted(one["statistic_id"] for one in written) == [
        recordings.COST_STATISTIC_ID,
        recordings.USAGE_STATISTIC_ID,
    ]
    assert flat_portal.poll_hourly.call_args.kwargs == {"time_of_use": False}
    sums = await statistic_sums(
        hass, recordings.USAGE_STATISTIC_ID, recordings.COST_STATISTIC_ID
    )
    assert sums[recordings.USAGE_STATISTIC_ID] == pytest.approx(RECORDED_KWH)
    # 157.55 kWh at 0.05 + 0.10, and 72 of the cycle's 744 hours of the $30
    # member charge: 23.6325 + 2.90323 = 26.53573.
    assert sums[recordings.COST_STATISTIC_ID] == pytest.approx(26.53573, abs=1e-5)
    assert [one.issue_id for one in issue_registry.issues.values()] == []
    keys = [
        one.unique_id
        for one in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    ]
    assert not any("allowance" in key for key in keys)


@pytest.mark.parametrize(
    ("effective_from", "expected"),
    [
        # Effective before the cycle ends, though after every recorded hour:
        # 157.55 kWh at 0.05 + 0.20, plus the same 2.90323 of member charge.
        pytest.param("2026-09-15", 42.29073, id="before_the_bill"),
        pytest.param("2026-09-28", 42.29073, id="on_the_bill_day"),
        pytest.param("2026-09-29", 26.53573, id="after_the_bill"),
    ],
)
async def test_the_version_on_the_bill_day_governs_the_whole_cycle(
    hass: HomeAssistant, flat_portal: AsyncMock, effective_from: str, expected: float
) -> None:
    """A tariff decides which version governs an hour, not the shared code."""
    entry = await configure(
        hass, [{**PUBLISHED, "effective_from": effective_from, "power_rate": 0.20}]
    )

    sums = await statistic_sums(hass, recordings.COST_STATISTIC_ID)
    assert entry.state is ConfigEntryState.LOADED
    assert sums[recordings.COST_STATISTIC_ID] == pytest.approx(expected, abs=1e-5)


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


async def test_a_flat_bill_is_finalized_with_its_own_lines(
    hass: HomeAssistant, flat_portal: AsyncMock
) -> None:
    """The bill's lines are the tariff's, and the cycle adds up to the bill."""
    entry = await configure(hass, [])

    await hass.services.async_call(
        DOMAIN,
        SERVICE_FINALIZE_CYCLE,
        finalize_call(entry, member_charge=29.0),
        blocking=True,
    )
    await async_wait_recording_done(hass)

    sums = await statistic_sums(hass, recordings.COST_STATISTIC_ID)
    assert sums[recordings.COST_STATISTIC_ID] == pytest.approx(40.0, abs=1e-6)
    # The bill's $29, not the schedule's $30, smeared over 72 of the 168 hours
    # to the read end: 23.6325 + 29 * 72 / 168 = 36.06107, and 40 - 36.06107.
    records = (await async_get_config_entry_diagnostics(hass, entry))["cycle_records"]
    assert records["2026-08-28"]["values"] == {"member_charge": 29.0}
    assert records["2026-08-28"]["residual"] == pytest.approx(3.93893, abs=1e-5)


async def test_a_flat_bill_refuses_niteflex_lines(
    hass: HomeAssistant, flat_portal: AsyncMock
) -> None:
    """A line this tariff does not read is a mistake, not something to ignore."""
    entry = await configure(hass, [])

    with pytest.raises(ServiceValidationError) as refused:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_FINALIZE_CYCLE,
            finalize_call(entry, member_charge=29.0, pca_factor=0.01),
            blocking=True,
        )

    assert refused.value.translation_key == "bill_line_unexpected"
    assert refused.value.translation_placeholders == {
        "field": "pca_factor",
        "tariff": FLAT.display_name,
    }
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["cycle_records"] == {}


async def test_a_bill_date_tariff_may_date_its_first_version_after_the_connect_date(
    hass: HomeAssistant, flat_portal: AsyncMock
) -> None:
    """The first cycle's bill is what the first version has to be in effect for."""
    entry = await configure(hass, [], effective_from="2026-09-15")

    assert entry.state is ConfigEntryState.LOADED
    sums = await statistic_sums(hass, recordings.COST_STATISTIC_ID)
    assert sums[recordings.COST_STATISTIC_ID] == pytest.approx(26.53573, abs=1e-5)


def test_a_usage_tariff_cannot_name_periods() -> None:
    """Periods only mean something to a tariff priced from time of use."""
    with pytest.raises(ValueError, match="names periods"):
        replace(FLAT, periods=NITEFLEX.periods)


def test_two_tariffs_matching_one_location_is_an_error() -> None:
    """Registry order never decides which rate schedule a member is billed under."""
    twin = replace(FLAT, key="twin")
    with (
        patch.dict(TARIFFS, {twin.key: twin}),
        pytest.raises(ValueError, match="all match"),
    ):
        tariff_for(HOST, ["B:FLAT"])


def test_a_published_version_has_to_conform() -> None:
    """A shipped default is held to the same rules as a typed one."""
    broken = RateVersion(effective_from=date(2026, 1, 1), values={"power_rate": 0.1})
    with pytest.raises(ValueError, match="missing"):
        replace(FLAT, published_versions=(broken,))
