from homeassistant.helpers.update_coordinator import CoordinatorEntity


class HouseholdEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, suffix):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{suffix}"
        self._attr_device_info = {"identifiers": {("household_planner", coordinator.entry.entry_id)},
                                  "name": coordinator.entry.title}

    @property
    def available(self):
        return self.coordinator.data is not None
