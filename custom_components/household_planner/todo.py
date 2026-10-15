from datetime import datetime
from homeassistant.components.todo import TodoListEntity, TodoItem, TodoItemStatus
from .entity import HouseholdEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([HouseholdTodo(hass.data["household_planner"][entry.entry_id])])


class HouseholdTodo(HouseholdEntity, TodoListEntity):
    _attr_name = "Today"
    _attr_supported_features = 0  # No native add/remove/update bypass; own completion action.

    def __init__(self, coordinator):
        super().__init__(coordinator, "today_todo")

    @property
    def todo_items(self):
        return [TodoItem(uid=t["occurrence_id"], summary=t["title"],
                         status=TodoItemStatus.COMPLETED if t["status"] == "completed" else TodoItemStatus.NEEDS_ACTION,
                         due=datetime.fromisoformat(t["due_at"]) if t["due_at"] else None)
                for t in self.coordinator.data["tasks"]]
