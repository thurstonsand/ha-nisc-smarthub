"""The NISC SmartHub integration."""

from datetime import date, datetime, timedelta
import math

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_CONFIG_ENTRY_ID, CONF_HOST, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import (
    ATTR_BILL_TOTAL,
    ATTR_CYCLE_START,
    ATTR_FROM,
    ATTR_PCA_FACTOR,
    ATTR_READ_END,
    ATTR_READ_START,
    ATTR_SERVICE_CHARGE,
    ATTR_TAX,
    DOMAIN,
    SERVICE_FINALIZE_CYCLE,
    SERVICE_RECONCILE,
    SERVICE_UNFINALIZE_CYCLE,
)
from .coordinator import NiscSmartHubCoordinator
from .cycles import local_midnight
from .smarthub import SmartHubClient
from .store import CycleStore, async_remove_records
from .tariff import CycleActuals

type NiscSmartHubConfigEntry = ConfigEntry[NiscSmartHubCoordinator]

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    DOMAIN
)

RECONCILE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_FROM): cv.date,
    }
)

FINALIZE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_CYCLE_START): cv.date,
        vol.Required(ATTR_READ_START): cv.date,
        vol.Required(ATTR_READ_END): cv.date,
        vol.Required(ATTR_SERVICE_CHARGE): vol.Coerce(float),
        vol.Required(ATTR_PCA_FACTOR): vol.Coerce(float),
        vol.Required(ATTR_TAX): vol.Coerce(float),
        vol.Required(ATTR_BILL_TOTAL): vol.Coerce(float),
    }
)

UNFINALIZE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_CYCLE_START): cv.date,
    }
)

BILL_FIELDS = (ATTR_SERVICE_CHARGE, ATTR_PCA_FACTOR, ATTR_TAX, ATTR_BILL_TOTAL)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions this integration exposes, once."""
    hass.services.async_register(
        DOMAIN, SERVICE_RECONCILE, _async_reconcile, schema=RECONCILE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_FINALIZE_CYCLE, _async_finalize_cycle, schema=FINALIZE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UNFINALIZE_CYCLE,
        _async_unfinalize_cycle,
        schema=UNFINALIZE_SCHEMA,
    )
    return True


async def _async_reconcile(call: ServiceCall) -> None:
    """Reconcile one entry's whole history, or everything from a day."""
    coordinator = _coordinator(call)
    day: date | None = call.data.get(ATTR_FROM)
    if day is not None and day > dt_util.now().date():
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="from_in_future",
            translation_placeholders={"from": day.isoformat()},
        )
    coordinator.request_full(None if day is None else _midnight(day))
    await _async_run(coordinator)


async def _async_finalize_cycle(call: ServiceCall) -> None:
    """Freeze one cycle to its posted bill and reprice it from there."""
    coordinator = _coordinator(call)
    day: date = call.data[ATTR_CYCLE_START]
    _require_cycle(coordinator, day)
    read_start = _midnight(call.data[ATTR_READ_START])
    read_end = _midnight(call.data[ATTR_READ_END])
    if read_start >= read_end:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="read_dates_out_of_order",
            translation_placeholders={
                "read_start": call.data[ATTR_READ_START].isoformat(),
                "read_end": call.data[ATTR_READ_END].isoformat(),
            },
        )
    if call.data[ATTR_READ_END] > dt_util.now().date():
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="read_end_in_future",
            translation_placeholders={"read_end": call.data[ATTR_READ_END].isoformat()},
        )
    overlapped = coordinator.overlapping_record(day, read_start, read_end)
    if overlapped is not None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="overlapping_cycle",
            translation_placeholders={"cycle_start": overlapped.isoformat()},
        )
    # The last poll's hours only cover its window, so a cycle older than that
    # cannot be priced here to check it; that the portal has reported the
    # cycle's last hour says every hour of the cycle has arrived.
    newest = coordinator.data.newest_data_hour
    if newest is None or read_end - timedelta(hours=1) > newest:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="cycle_has_no_priced_hours",
            translation_placeholders={
                "read_end": call.data[ATTR_READ_END].isoformat(),
                "newest_data_hour": "nothing" if newest is None else newest.isoformat(),
            },
        )

    await coordinator.async_finalize_cycle(
        day,
        CycleActuals(
            read_start=read_start,
            read_end=read_end,
            service_charge=_finite(call, ATTR_SERVICE_CHARGE),
            pca_factor=_finite(call, ATTR_PCA_FACTOR),
            tax=_finite(call, ATTR_TAX),
            bill_total=_finite(call, ATTR_BILL_TOTAL),
        ),
    )
    await _async_run(coordinator)


async def _async_unfinalize_cycle(call: ServiceCall) -> None:
    """Drop one cycle's posted bill and price it from the schedule again."""
    coordinator = _coordinator(call)
    day: date = call.data[ATTR_CYCLE_START]
    if coordinator.cycle_records.get(day) is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="cycle_not_finalized",
            translation_placeholders={"cycle_start": day.isoformat()},
        )

    await coordinator.async_unfinalize_cycle(day)
    await _async_run(coordinator)


async def _async_run(coordinator: NiscSmartHubCoordinator) -> None:
    """Reconcile now, and tell the caller when the run could not finish."""
    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="reconcile_failed",
            translation_placeholders={"error": str(coordinator.last_exception)},
        )


def _coordinator(call: ServiceCall) -> NiscSmartHubCoordinator:
    entry_id: str = call.data[ATTR_CONFIG_ENTRY_ID]
    entry = call.hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_entry",
            translation_placeholders={"entry_id": entry_id},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"target": entry.title},
        )
    return entry.runtime_data


def _require_cycle(coordinator: NiscSmartHubCoordinator, day: date) -> None:
    if coordinator.cycle_starting(day) is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_cycle",
            translation_placeholders={"cycle_start": day.isoformat()},
        )


def _finite(call: ServiceCall, field: str) -> float:
    value: float = call.data[field]
    if not math.isfinite(value):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_a_finite_number",
            translation_placeholders={"field": field, "value": str(value)},
        )
    return value


def _midnight(day: date) -> datetime:
    return local_midnight(day, dt_util.get_default_time_zone())


async def async_setup_entry(
    hass: HomeAssistant, entry: NiscSmartHubConfigEntry
) -> bool:
    """Set up one service location."""
    local_zone = dt_util.get_default_time_zone()
    client = SmartHubClient(
        async_get_clientsession(hass), entry.data[CONF_HOST], local_zone
    )
    store = CycleStore(hass, entry_id=entry.entry_id, zone=local_zone)
    await store.async_load()
    coordinator = NiscSmartHubCoordinator(hass, entry, client, local_zone, store)
    await coordinator.async_config_entry_first_refresh()

    # The statistics are the product, not the sensors, and a coordinator only
    # polls while something listens.
    entry.async_on_unload(coordinator.async_add_listener(lambda: None))
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: NiscSmartHubConfigEntry
) -> bool:
    """Unload one service location."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(
    hass: HomeAssistant, entry: NiscSmartHubConfigEntry
) -> None:
    """Drop the cycle records this entry owned."""
    await async_remove_records(hass, entry.entry_id)
