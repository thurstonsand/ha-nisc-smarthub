"""What a posted bill said about one cycle, kept beside the config entry.

Configuration lives in the entry; observed facts live here. A record exists
only for a finalized cycle: its read dates replace the calendar bounds, its
service charge, rider factor and tax replace the schedule's estimates, and the
residual is what the bill total left over once the components were priced.

Read dates are stored as calendar days and read back as local midnight, because
a bill states days and a cycle is bounded by instants.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, tzinfo
import math
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .cycles import local_midnight
from .tariff import Cycle, CycleActuals

STORAGE_VERSION: Final = 1
RESIDUAL_TOLERANCE: Final = 1e-6


@dataclass(frozen=True, kw_only=True)
class CycleRecord:
    """One finalized cycle: the bill's values, and what pricing them left over.

    `residual` is `None` until a run has priced the cycle, which is the only
    thing that can compute it.
    """

    actuals: CycleActuals
    residual: float | None


def store_key(entry_id: str) -> str:
    """Return the storage key one entry's records live under."""
    return f"{DOMAIN}.{entry_id}"


async def async_remove_records(hass: HomeAssistant, entry_id: str) -> None:
    """Delete one entry's stored cycle records."""
    await Store[dict[str, Any]](
        hass, STORAGE_VERSION, store_key(entry_id)
    ).async_remove()


class CycleStore:
    """The finalized cycles of one config entry."""

    def __init__(self, hass: HomeAssistant, *, entry_id: str, zone: tzinfo) -> None:
        """Initialize the store for one entry, reading dates in its zone."""
        self._store = Store[dict[str, Any]](hass, STORAGE_VERSION, store_key(entry_id))
        self._zone = zone
        self._records: dict[date, CycleRecord] = {}

    async def async_load(self) -> None:
        """Read the records from disk, or start with none."""
        stored = await self._store.async_load()
        if stored is None:
            return
        cycles: dict[str, Any] = stored["cycles"]
        self._records = {
            date.fromisoformat(day): self._record(one) for day, one in cycles.items()
        }

    @property
    def records(self) -> dict[date, CycleRecord]:
        """Return every finalized cycle, by the day that identifies it."""
        return dict(self._records)

    def spans(self) -> list[Cycle]:
        """Return each finalized cycle's bounds, oldest first."""
        return [
            Cycle(
                start=record.actuals.read_start,
                end=record.actuals.read_end,
                partial=False,
            )
            for _, record in sorted(self._records.items())
        ]

    def actuals_by_start(self) -> dict[datetime, CycleActuals]:
        """Return each finalized cycle's actuals, keyed by where it starts."""
        return {
            record.actuals.read_start: record.actuals
            for record in self._records.values()
        }

    def days_by_start(self) -> dict[datetime, date]:
        """Return the day identifying each finalized cycle, keyed by its start."""
        return {record.actuals.read_start: day for day, record in self._records.items()}

    def earliest_unpriced_start(self) -> datetime | None:
        """Return where the oldest cycle whose bill no run has priced yet starts."""
        return min(
            (
                record.actuals.read_start
                for record in self._records.values()
                if record.residual is None
            ),
            default=None,
        )

    def actuals_for(self, day: date) -> CycleActuals | None:
        """Return what the bill said about the cycle starting on a day."""
        record = self._records.get(day)
        return None if record is None else record.actuals

    async def async_finalize(self, day: date, actuals: CycleActuals) -> None:
        """Record a posted bill, dropping any residual computed before it."""
        self._records[day] = CycleRecord(actuals=actuals, residual=None)
        await self._async_save()

    async def async_unfinalize(self, day: date) -> None:
        """Forget one cycle's bill, so the schedule prices it again."""
        del self._records[day]
        await self._async_save()

    async def async_set_residuals(self, residuals: Mapping[date, float]) -> None:
        """Store what each priced cycle's bill left over, if it moved."""
        changed = False
        for day, residual in residuals.items():
            record = self._records.get(day)
            if record is None or _settled(record.residual, residual):
                continue
            self._records[day] = replace(record, residual=residual)
            changed = True
        if changed:
            await self._async_save()

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "cycles": {
                    day.isoformat(): _stored(record)
                    for day, record in sorted(self._records.items())
                }
            }
        )

    def _record(self, stored: dict[str, Any]) -> CycleRecord:
        residual = stored["residual"]
        return CycleRecord(
            actuals=CycleActuals(
                read_start=local_midnight(
                    date.fromisoformat(stored["read_start"]), self._zone
                ),
                read_end=local_midnight(
                    date.fromisoformat(stored["read_end"]), self._zone
                ),
                service_charge=float(stored["service_charge"]),
                pca_factor=float(stored["pca_factor"]),
                tax=float(stored["tax"]),
                bill_total=float(stored["bill_total"]),
            ),
            residual=None if residual is None else float(residual),
        )


def _settled(before: float | None, after: float) -> bool:
    if before is None:
        return False
    return math.isclose(before, after, abs_tol=RESIDUAL_TOLERANCE)


def _stored(record: CycleRecord) -> dict[str, Any]:
    actuals = record.actuals
    return {
        "read_start": actuals.read_start.date().isoformat(),
        "read_end": actuals.read_end.date().isoformat(),
        "service_charge": actuals.service_charge,
        "pca_factor": actuals.pca_factor,
        "tax": actuals.tax,
        "bill_total": actuals.bill_total,
        "residual": record.residual,
    }
