"""HA transport for one autonomous deterministic household engine.

No new HTTP server. Trusted identity comes only from HA's authenticated Context.
Domain actions deny absent/system user contexts; native todo remains read-only.
"""
from __future__ import annotations
import asyncio
from datetime import timedelta
from functools import partial
import time
import voluptuous as vol

from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .donetick import Donetick, VERIFIED_BUILD, credential_provider
from .engine import Engine, HouseholdError
from .storage import LocalStore
from . import engine as engine_module, planning as planning_module
from zoneinfo import ZoneInfo
from homeassistant.components.http import StaticPathConfig
from pathlib import Path

DOMAIN = "household_planner"
PLATFORMS = ["todo", "sensor", "binary_sensor"]
READ = {"read"}
WRITE = READ | {"sync", "generate", "complete", "replan"}
MANAGE = WRITE | {"manage"}
SERVICE_FIELDS = {
    "get_today": {}, "sync": {}, "generate_today_plan": {},
    "complete_task": {vol.Required("plan_id"): str, vol.Required("occurrence_id"): str,
                      vol.Required("plan_revision"): vol.All(int, vol.Range(min=1)), vol.Required("request_id"): str},
    "complete_extra_task": {vol.Required("task_id"): str,
                            vol.Required("sync_version"): vol.All(int, vol.Range(min=1)), vol.Required("request_id"): str},
    "replan_today": {vol.Required("plan_id"): str, vol.Required("plan_revision"): vol.All(int, vol.Range(min=1)),
                     vol.Optional("dry_run", default=True): bool, vol.Optional("preview_id"): str,
                     vol.Optional("request_id"): str},
    "cancel_pending": {vol.Required("request_id"): str},
    "get_settings": {},
    "set_week_budget": {vol.Required("budgets"): vol.All([vol.All(int, vol.Range(min=0))], vol.Length(min=7, max=7))},
    "set_task_metadata": {vol.Required("task_id"): str, vol.Required("weight"): vol.Any(None, vol.In([1, 2, 3])),
                          vol.Required("area"): str, vol.Required("contains_vacuuming"): vol.Any(None, bool),
                          vol.Optional("fixed_date"): vol.Any(None, str), vol.Optional("advance_days", default=0): int},
    "manage_task": {vol.Required("request_id"): str, vol.Required("operation"): vol.In(["save", "archive"]),
                    vol.Optional("fields"): dict, vol.Optional("task_id"): str},
}


class HouseholdCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, engine):
        import logging
        super().__init__(hass, logging.getLogger(__name__), name=DOMAIN, update_interval=None)
        self.engine, self.entry = engine, entry
        self.action_lock = asyncio.Lock()
        self.next_sync = 0.0
        self.pending_retry_at = 0.0
        self.backoff = 300
        self.engine.permissions["__scheduler__"] = MANAGE.copy()
        self.engine.write_principal_check = self.check_write_principal
        self.unsub = None

    def check_write_principal(self, uid):
        # Called ONLY in the executor immediately before the source POST. A user
        # disabled/deleted during the pending window must not retain queued writes.
        try:
            user = asyncio.run_coroutine_threadsafe(
                self.hass.auth.async_get_user(uid), self.hass.loop).result(timeout=2)
            if user and user.is_active and not user.system_generated:
                return
        except Exception:
            pass
        raise HouseholdError("authenticated_user_required")

    async def run(self, fn, *args, **kwargs):
        # Serialize executor calls across service handlers, scheduler and shutdown.
        async with self.action_lock:
            return await self.hass.async_add_executor_job(partial(fn, *args, **kwargs))

    async def refresh_projection(self):
        snapshot = await self.run(self.engine.get_today, "__scheduler__")
        self.async_set_updated_data(snapshot)

    async def tick(self, _now=None):
        if self.action_lock.locked():
            return
        pending = any(r["phase"] == "pending_cancelable" for r in self.engine.state["requests"].values())
        now = time.monotonic()
        try:
            if pending and now >= self.pending_retry_at:
                await self.run(self.engine.flush_pending, "__scheduler__")
                self.next_sync = now+300
                self.pending_retry_at = 0.0
                self.backoff = 300
            elif now >= self.next_sync:
                await self.run(self.engine.sync, "__scheduler__")
                self.next_sync = now+300
                self.backoff = 300
            # Idempotent startup/missed-midnight recovery, never fills an existing plan.
            if self.engine.state["budgets"] is not None:
                await self.run(self.engine.generate, "__scheduler__")
        except HouseholdError:
            self.next_sync = now+self.backoff
            self.pending_retry_at = now+self.backoff
            self.backoff = min(3600, self.backoff*2)
        finally:
            await self.refresh_projection()


async def async_setup_entry(hass, entry):
    store = None
    try:
        # Writes remain restricted to the exact companion backend build.
        source = await hass.async_add_executor_job(partial(
            Donetick, entry.data["base_url"], credential_provider(entry.data["api_key"]),
            expected_version=entry.data["expected_version"], write_verified=True,
            required_build=VERIFIED_BUILD))
        engine_module.TZ = ZoneInfo(hass.config.time_zone)
        blocked = 6 if entry.options.get("no_sunday_vacuum", entry.data.get("no_sunday_vacuum", True)) else None
        engine_module.NO_VACUUM_WEEKDAY = planning_module.NO_VACUUM_WEEKDAY = blocked
        store = await hass.async_add_executor_job(LocalStore, hass.config.path(".storage", DOMAIN, entry.entry_id))
        engine = Engine(store, source)
        engine.gentle_smoothing = entry.options.get("gentle_smoothing", False)
        for field, grants in (("reader_ids", READ), ("writer_ids", WRITE), ("manager_ids", MANAGE)):
            for uid in entry.data.get(field, "").split(","):
                uid = uid.strip()
                if uid and uid != "__scheduler__":
                    engine.permissions.setdefault(uid, set()).update(grants)
        if engine.state["budgets"] is None:
            engine.permissions["__setup__"] = MANAGE.copy()
            engine.set_budget("__setup__", entry.data["budgets"])
            del engine.permissions["__setup__"]
        if not hass.data.get(DOMAIN + "_static"):
            await hass.http.async_register_static_paths([StaticPathConfig("/household_planner/card.js", str(Path(__file__).parent / "card.js"), False)])
            hass.data[DOMAIN + "_static"] = True
        coordinator = HouseholdCoordinator(hass, entry, engine)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
        # Polling failure retains explicitly stale readable snapshots instead of dropping history.
        await coordinator.tick()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        coordinator.unsub = async_track_time_interval(hass, coordinator.tick, timedelta(seconds=10))
    except Exception as exc:
        if store is not None:
            await hass.async_add_executor_job(store.close)
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        code = str(exc) if isinstance(exc, HouseholdError) else "household_setup_failed"
        raise ConfigEntryNotReady(code) from None
    if not hass.services.has_service(DOMAIN, "get_today"):
        for name, fields in SERVICE_FIELDS.items():
            schema = vol.Schema({vol.Required("config_entry_id"): str, **fields}, extra=vol.PREVENT_EXTRA)
            hass.services.async_register(DOMAIN, name, partial(handle_service, hass, name),
                                         schema=schema, supports_response=SupportsResponse.ONLY)
    entry.async_on_unload(entry.add_update_listener(async_options_updated))
    return True


async def async_options_updated(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def handle_service(hass, name, call):
    coordinator = hass.data.get(DOMAIN, {}).get(call.data["config_entry_id"])
    if coordinator is None:
        raise ServiceValidationError("household_entry_not_loaded")
    uid = call.context.user_id
    if not uid:
        raise ServiceValidationError("authenticated_user_required")
    user = await hass.auth.async_get_user(uid)
    if not user or not user.is_active or user.system_generated:
        raise ServiceValidationError("authenticated_user_required")
    e = coordinator.engine
    args = dict(call.data)
    args.pop("config_entry_id")
    mapping = {
        "get_today": (e.get_today, "read"), "sync": (e.sync, "sync"),
        "generate_today_plan": (e.generate, "generate"), "complete_task": (e.complete, "complete"),
        "complete_extra_task": (e.complete_extra, "complete"),
        "replan_today": (e.replan, "replan"), "cancel_pending": (e.cancel_pending, "complete"),
        "get_settings": (e.list_tasks, "manage"), "set_week_budget": (e.set_budget, "manage"),
        "set_task_metadata": (e.set_metadata, "manage"), "manage_task": (e.manage_task, "manage"),
    }
    fn, action = mapping[name]
    try:
        e.authorize(uid, action)
        if "plan_revision" in args:
            args["revision"] = args.pop("plan_revision")
        if name == "set_week_budget":
            args["values"] = args.pop("budgets")
        result = await coordinator.run(fn, uid, **args)
        if name != "get_today":
            await coordinator.refresh_projection()
        return result
    except HouseholdError as exc:
        raise ServiceValidationError(str(exc)) from None
    except Exception:
        raise HomeAssistantError("household_operation_failed") from None


async def async_unload_entry(hass, entry):
    c = hass.data[DOMAIN][entry.entry_id]
    if c.unsub:
        c.unsub()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await c.run(c.engine.store.close)
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            for name in SERVICE_FIELDS:
                hass.services.async_remove(DOMAIN, name)
    return unloaded
