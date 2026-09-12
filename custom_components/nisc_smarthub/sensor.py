"""What the last reconcile run leaves worth looking at."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NiscSmartHubConfigEntry
from .const import CURRENCY_UNIT
from .coordinator import NiscSmartHubCoordinator, SmartHubData
from .entity import NiscSmartHubEntity
from .tariff import Period

# Read-only platform: the coordinator already serializes the fetch.
PARALLEL_UPDATES = 0

MONEY_PLACES = 4


@dataclass(frozen=True, kw_only=True)
class NiscSmartHubSensorEntityDescription(SensorEntityDescription):
    """Describes a sensor and how to read it from one reconcile run."""

    value_fn: Callable[[SmartHubData], float | datetime | None]
    attributes_fn: Callable[[SmartHubData], dict[str, Any]] | None = None


def _cost_breakdown(data: SmartHubData) -> dict[str, Any]:
    """Return the cycle's cost components and how much of it is priced."""
    breakdown = data.cycle_cost
    return {
        "energy": round(breakdown.energy, MONEY_PLACES),
        "fixed": round(breakdown.fixed, MONEY_PLACES),
        "rider": round(breakdown.rider, MONEY_PLACES),
        "tax": round(breakdown.tax, MONEY_PLACES),
        "adjustment": round(breakdown.adjustment, MONEY_PLACES),
        "residual": round(breakdown.residual, MONEY_PLACES),
        "priced_hours": data.cycle_priced_hours,
        "unpriced_hours": data.cycle_unpriced_hours,
    }


# The cycle sensors carry no state class. They restate what the statistics
# already hold, hour by hour, and a state class would have the recorder build
# a second, coarser long-term history of the same numbers from their states.
SENSORS: tuple[NiscSmartHubSensorEntityDescription, ...] = (
    NiscSmartHubSensorEntityDescription(
        key="cycle_usage",
        translation_key="cycle_usage",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda data: data.cycle_usage,
    ),
    NiscSmartHubSensorEntityDescription(
        key="cycle_cost",
        translation_key="cycle_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_UNIT,
        suggested_display_precision=2,
        value_fn=lambda data: data.cycle_cost.total,
        attributes_fn=_cost_breakdown,
    ),
    NiscSmartHubSensorEntityDescription(
        key="last_poll",
        translation_key="last_poll",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.last_poll,
    ),
    NiscSmartHubSensorEntityDescription(
        key="newest_data_hour",
        translation_key="newest_data_hour",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.newest_data_hour,
    ),
)


def allowance_description(period: Period) -> NiscSmartHubSensorEntityDescription:
    """Describe the sensor for one tiered period's remaining allowance."""
    key = f"allowance_remaining_{period.slug}"
    return NiscSmartHubSensorEntityDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda data: data.allowance_remaining.get(period.slug),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NiscSmartHubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors of one service location."""
    coordinator = entry.runtime_data
    tiered = [
        period
        for period in coordinator.tariff.periods
        if period.slug in coordinator.tariff.tiered_periods
    ]
    async_add_entities(
        NiscSmartHubSensor(coordinator, description)
        for description in SENSORS
        + tuple(allowance_description(period) for period in tiered)
    )


# `SensorEntity` redeclares members `Entity` already declares and narrows
# `entity_description`, which strict mode reads as an incompatible override
# on every integration entity built from the pair.
class NiscSmartHubSensor(SensorEntity, NiscSmartHubEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """One number or moment from the last reconcile run."""

    entity_description: NiscSmartHubSensorEntityDescription  # pyright: ignore[reportIncompatibleVariableOverride]

    def __init__(
        self,
        coordinator: NiscSmartHubCoordinator,
        description: NiscSmartHubSensorEntityDescription,
    ) -> None:
        """Initialize the sensor its description describes."""
        super().__init__(coordinator, description.key)
        self.entity_description = description  # pyright: ignore[reportIncompatibleVariableOverride]

    @property
    def native_value(self) -> float | datetime | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return what the last run left for this sensor."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return whatever this sensor carries alongside its state."""
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.coordinator.data)
