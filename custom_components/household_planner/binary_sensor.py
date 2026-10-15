from homeassistant.components.binary_sensor import BinarySensorEntity
from .entity import HouseholdEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([HouseholdReady(hass.data["household_planner"][entry.entry_id])])


class HouseholdReady(HouseholdEntity, BinarySensorEntity):
    _attr_name = "Ready"

    def __init__(self, coordinator):
        super().__init__(coordinator, "ready")

    @property
    def is_on(self):
        return self.coordinator.data["planner_state"] in ("ready", "conflict")
