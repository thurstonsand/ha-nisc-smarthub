"""What to hand someone debugging this entry, with the member taken out.

Redaction runs twice over the dump: once by key, for the credentials, and once
by value, because the account number, the service location id and the meter
number are spelled into statistic ids and issue placeholders where no key names
them. The cycle records carry money and dates only, and are dumped whole.
"""

from collections.abc import Mapping
from datetime import date
from typing import Any

from homeassistant.const import CONF_EMAIL, CONF_LOCATION, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.redact import (
    async_redact_data,  # pyright: ignore[reportUnknownVariableType]  # its overloads are generic over an unparameterized Mapping
)

from . import NiscSmartHubConfigEntry
from .const import CONF_ACCOUNT, CONF_TOTP_SECRET, DOMAIN
from .coordinator import SmartHubData, WindowPlan
from .store import CycleRecord

TO_REDACT = {CONF_PASSWORD, CONF_TOTP_SECRET, CONF_EMAIL}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NiscSmartHubConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one service location."""
    coordinator = entry.runtime_data
    data = coordinator.data
    dump: dict[str, Any] = {
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": async_redact_data(entry.data, TO_REDACT),
            "options": dict(entry.options),
        },
        "tariff": coordinator.tariff.key,
        "statistic_ids": coordinator.statistic_ids,
        "last_update_success": coordinator.last_update_success,
        "data": _run(data),
        "window_plan": _plan(coordinator.last_plan),
        "issues": _issues(hass, entry.entry_id),
        "cycle_records": _records(coordinator.cycle_records),
    }
    return redact_identifiers(
        dump,
        {
            "account": entry.data[CONF_ACCOUNT],
            "location": entry.data[CONF_LOCATION],
            "meter": data.meter,
        },
    )


def redact_identifiers(payload: Any, identifiers: Mapping[str, str]) -> Any:
    """Replace every occurrence of each identifier, wherever it is spelled."""
    if isinstance(payload, str):
        return _replaced(payload, identifiers)
    if isinstance(payload, Mapping):
        return {
            _replaced(key, identifiers) if isinstance(key, str) else key: (
                redact_identifiers(value, identifiers)
            )
            for key, value in payload.items()  # pyright: ignore[reportUnknownVariableType]
        }
    if isinstance(payload, list | tuple):
        return [redact_identifiers(one, identifiers) for one in payload]  # pyright: ignore[reportUnknownVariableType]
    return payload


def _replaced(value: str, identifiers: Mapping[str, str]) -> str:
    for kind, identifier in identifiers.items():
        value = value.replace(identifier, f"**REDACTED_{kind}**")
    return value


def _run(data: SmartHubData) -> dict[str, Any]:
    return {
        "last_poll": data.last_poll.isoformat(),
        "newest_data_hour": (
            None if data.newest_data_hour is None else data.newest_data_hour.isoformat()
        ),
        "meter": data.meter,
        "cycle": {
            "start": data.cycle.start.isoformat(),
            "end": data.cycle.end.isoformat(),
            "partial": data.cycle.partial,
        },
        "cycle_usage": data.cycle_usage,
        "cycle_cost": {
            "energy": data.cycle_cost.energy,
            "fixed": data.cycle_cost.fixed,
            "rider": data.cycle_cost.rider,
            "tax": data.cycle_cost.tax,
            "adjustment": data.cycle_cost.adjustment,
            "residual": data.cycle_cost.residual,
            "total": data.cycle_cost.total,
        },
        "cycle_priced_hours": data.cycle_priced_hours,
        "cycle_unpriced_hours": data.cycle_unpriced_hours,
        "allowance_remaining": dict(data.allowance_remaining),
    }


def _records(records: Mapping[date, CycleRecord]) -> dict[str, Any]:
    return {
        day.isoformat(): {
            "read_start": record.actuals.read_start.isoformat(),
            "read_end": record.actuals.read_end.isoformat(),
            "service_charge": record.actuals.service_charge,
            "pca_factor": record.actuals.pca_factor,
            "tax": record.actuals.tax,
            "bill_total": record.actuals.bill_total,
            "residual": record.residual,
        }
        for day, record in sorted(records.items())
    }


def _plan(plan: WindowPlan | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "tier": str(plan.tier),
        "start": plan.start.isoformat(),
        "end": plan.end.isoformat(),
        "cycles": [
            {
                "start": cycle.start.isoformat(),
                "end": cycle.end.isoformat(),
                "partial": cycle.partial,
            }
            for cycle in plan.cycles
        ],
    }


def _issues(hass: HomeAssistant, entry_id: str) -> list[dict[str, Any]]:
    return [
        {
            "issue_id": issue.issue_id,
            "severity": str(issue.severity),
            "translation_placeholders": issue.translation_placeholders,
        }
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN and issue_id.endswith(entry_id)
    ]
