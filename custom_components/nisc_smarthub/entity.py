"""What every entity of one config entry has in common."""

from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST, CONF_LOCATION
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ACCOUNT, DOMAIN
from .coordinator import NiscSmartHubCoordinator

# The host's first label is the co-op's portal slug, which title-cases into
# something no co-op calls itself. NISC is the vendor behind every SmartHub
# portal, and the co-op is already named by the model.
MANUFACTURER = "NISC SmartHub"


class NiscSmartHubEntity(CoordinatorEntity[NiscSmartHubCoordinator]):
    """One entity of the service location this entry tracks."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NiscSmartHubCoordinator, key: str) -> None:
        """Place the entity on this entry's device under its own unique id."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        if TYPE_CHECKING:
            assert entry is not None
        location = f"{entry.data[CONF_ACCOUNT]}_{entry.data[CONF_LOCATION]}"
        self._attr_unique_id = f"{location}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, location)},
            entry_type=DeviceEntryType.SERVICE,
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=coordinator.tariff.display_name,
            serial_number=coordinator.data.meter,
            configuration_url=f"https://{entry.data[CONF_HOST]}/",
        )
