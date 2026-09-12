"""The per-entry record of what each posted bill said."""

from datetime import date
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.nisc_smarthub.store import (
    CycleStore,
    async_remove_records,
    store_key,
)
from custom_components.nisc_smarthub.tariff import CycleActuals

from . import recordings

ENTRY_ID = "01JABCDEF0123456789"

AUGUST = date(2026, 8, 28)


def actuals(**overrides: Any) -> CycleActuals:
    """Return one bill's values, read dates at local midnight."""
    fields: dict[str, Any] = {
        "read_start": recordings.CONNECT_DATE,
        "read_end": recordings.CONNECT_DATE.replace(month=9, day=25),
        "service_charge": 33.0,
        "pca_factor": 0.012,
        "tax": 1.5,
        "bill_total": 25.0,
        **overrides,
    }
    return CycleActuals(**fields)


def store(hass: HomeAssistant) -> CycleStore:
    """Return a store over the test entry's records."""
    return CycleStore(hass, entry_id=ENTRY_ID, zone=recordings.PORTAL_ZONE)


async def test_a_store_with_no_file_holds_no_records(hass: HomeAssistant) -> None:
    """An entry that has never seen a bill starts empty."""
    loaded = store(hass)
    await loaded.async_load()

    assert loaded.records == {}
    assert loaded.spans() == []
    assert loaded.actuals_for(AUGUST) is None


async def test_a_finalized_cycle_survives_a_reload(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """What one store wrote is what the next one reads."""
    written = store(hass)
    await written.async_load()
    await written.async_finalize(AUGUST, actuals())
    await written.async_set_residuals({AUGUST: 1.25})

    assert hass_storage[store_key(ENTRY_ID)]["data"] == {
        "cycles": {
            "2026-08-28": {
                "read_start": "2026-08-28",
                "read_end": "2026-09-25",
                "service_charge": 33.0,
                "pca_factor": 0.012,
                "tax": 1.5,
                "bill_total": 25.0,
                "residual": 1.25,
            }
        }
    }

    read = store(hass)
    await read.async_load()

    assert read.records == written.records
    assert read.actuals_for(AUGUST) == actuals()
    assert read.records[AUGUST].residual == 1.25


async def test_read_dates_come_back_as_local_midnight(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A bill states days; a cycle is bounded by instants in the portal's zone."""
    hass_storage[store_key(ENTRY_ID)] = {
        "version": 1,
        "minor_version": 1,
        "key": store_key(ENTRY_ID),
        "data": {
            "cycles": {
                "2026-08-28": {
                    "read_start": "2026-08-28",
                    "read_end": "2026-09-25",
                    "service_charge": 33,
                    "pca_factor": 0,
                    "tax": 1.5,
                    "bill_total": 25,
                    "residual": None,
                }
            }
        },
    }

    loaded = store(hass)
    await loaded.async_load()

    [span] = loaded.spans()
    assert span.start == recordings.CONNECT_DATE
    assert span.end == recordings.CONNECT_DATE.replace(month=9, day=25)
    assert span.partial is False
    assert loaded.days_by_start() == {recordings.CONNECT_DATE: AUGUST}
    assert loaded.actuals_by_start()[recordings.CONNECT_DATE].service_charge == 33.0
    assert loaded.records[AUGUST].residual is None


async def test_a_residual_that_has_not_moved_is_not_rewritten(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Every run reprices a finalized cycle; only a changed residual is stored."""
    written = store(hass)
    await written.async_load()
    await written.async_finalize(AUGUST, actuals())
    await written.async_set_residuals({AUGUST: 1.25})
    stored = hass_storage[store_key(ENTRY_ID)]["data"]

    await written.async_set_residuals({AUGUST: 1.25 + 1e-9})
    assert hass_storage[store_key(ENTRY_ID)]["data"] is stored

    await written.async_set_residuals({AUGUST: 2.0, date(2026, 9, 25): 9.0})
    assert written.records[AUGUST].residual == 2.0
    assert hass_storage[store_key(ENTRY_ID)]["data"] is not stored


async def test_unfinalizing_forgets_the_cycle(hass: HomeAssistant) -> None:
    """Dropping a bill leaves the cycle to the rate schedule again."""
    written = store(hass)
    await written.async_load()
    await written.async_finalize(AUGUST, actuals())
    await written.async_unfinalize(AUGUST)

    read = store(hass)
    await read.async_load()

    assert read.records == {}


async def test_removing_the_records_removes_the_file(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """An entry the user deleted leaves nothing behind."""
    written = store(hass)
    await written.async_load()
    await written.async_finalize(AUGUST, actuals())

    await async_remove_records(hass, ENTRY_ID)

    assert store_key(ENTRY_ID) not in hass_storage


async def test_finalizing_twice_drops_the_residual_of_the_first_bill(
    hass: HomeAssistant,
) -> None:
    """A corrected bill has not been priced yet, so it carries no residual."""
    written = store(hass)
    await written.async_load()
    await written.async_finalize(AUGUST, actuals())
    await written.async_set_residuals({AUGUST: 1.25})

    await written.async_finalize(AUGUST, actuals(bill_total=30.0))

    assert written.records[AUGUST].residual is None
    assert written.actuals_for(AUGUST) == actuals(bill_total=30.0)
