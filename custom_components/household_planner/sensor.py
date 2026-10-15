from homeassistant.components.sensor import SensorEntity
from .entity import HouseholdEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([HouseholdStatus(hass.data["household_planner"][entry.entry_id])])


class HouseholdStatus(HouseholdEntity, SensorEntity):
    _attr_name = "Today"

    def __init__(self, coordinator):
        super().__init__(coordinator, "today_sensor")

    @property
    def native_value(self):
        return self.coordinator.data["planner_state"]

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data
        keys = ("api_version", "household_id", "plan_id", "plan_revision", "date", "timezone",
                "last_sync", "last_plan_generation", "last_error_code")
        return {**{k: d[k] for k in keys}, **d["summary"], "conflict_count": len(d["conflicts"])}
