"""Deterministic household planner. No HA/LLM imports; one SQLite owner.

Donetick is authoritative. This store retains metadata and immutable plan snapshots,
not an independent editable task catalogue. All methods run under one process lock.
"""
from __future__ import annotations

import copy
import calendar
import hashlib
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .planning import canonical_type, room, schedule
NO_VACUUM_WEEKDAY = 6

TZ = ZoneInfo("Europe/Zurich")
API_VERSION = "1.0"
MAX_TASKS = 250
MAX_HISTORY = 2000


class HouseholdError(Exception):
    """Stable, sanitized code only; source payloads never enter error messages."""


def fail(code):
    raise HouseholdError(code)


def next_cleaning(last, recurrence, frequency):
    try:
        start = datetime.strptime(last, "%Y-%m-%d").replace(hour=12, tzinfo=TZ)
        if start.strftime("%Y-%m-%d") != last:
            fail("invalid_last_cleaning")
    except (TypeError, ValueError):
        fail("invalid_last_cleaning")
    if not integer(frequency, 1, 365):
        fail("invalid_frequency")
    if recurrence in ("daily", "weekly"):
        due = start + timedelta(days=frequency * (7 if recurrence == "weekly" else 1))
    elif recurrence in ("monthly", "yearly"):
        index = start.year * 12 + start.month - 1 + frequency * (12 if recurrence == "yearly" else 1)
        year, month = index // 12, index % 12 + 1
        if year > 9999:
            fail("invalid_last_cleaning")
        due = start.replace(year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1]))
    else:
        fail("unsupported_last_cleaning_recurrence")
    return due.astimezone(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def integer(value, lo, hi):
    return type(value) is int and lo <= value <= hi


def stamp(value):
    if not isinstance(value, str):
        fail("invalid_timestamp")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            fail("timezone_required")
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        fail("invalid_timestamp")


def text(value, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        fail("invalid_text")
    return value


def notes(value):
    """Plain-text task instructions; empty explicitly clears the note."""
    if not isinstance(value, str) or len(value) > 4000 or "\x00" in value:
        fail("invalid_task_notes")
    return value


class Store:
    def __init__(self, connection):
        self.db = connection
        self.db.execute("CREATE TABLE IF NOT EXISTS household_state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
        self.db.commit()

    def load(self):
        row = self.db.execute("SELECT body FROM household_state WHERE id=1").fetchone()
        if row:
            state = json.loads(row[0])
            if state.get("schema") != 1:
                fail("unsupported_store_schema")
            return state
        return {"schema": 1, "budgets": None, "config_revision": 0, "metadata": {},
                "source": {}, "history": {}, "occurrences": {}, "plans": {},
                "requests": {}, "previews": {}, "last_sync": None,
                "last_error_code": None, "source_revision": None}

    def save(self, state):
        body = json.dumps(state, ensure_ascii=False, allow_nan=False)
        if len(body.encode()) > 8 * 1024 * 1024:
            fail("store_limit")
        with self.db:
            self.db.execute("INSERT INTO household_state VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (body,))

    def close(self):
        self.db.close()

    def backup(self, destination):
        """Use SQLite backup API on a NAS-local destination, never copy a live DB."""
        self.db.backup(destination)


class Engine:
    def __init__(self, store, source, household_id="household-planner", clock=None):
        self.store, self.source = store, source
        self.household_id = household_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = threading.RLock()
        self.state = store.load()
        self.durable_state = copy.deepcopy(self.state)
        self.permissions = {}  # configured trusted transport principals; never client fields
        self.write_principal_check = None  # installed transport revalidates at final source boundary

    def now(self):
        value = self.clock()
        if value.tzinfo is None:
            fail("timezone_required")
        return value.astimezone(TZ)

    def authorize(self, principal, action):
        if action not in self.permissions.get(principal, set()):
            fail("forbidden")

    def save(self):
        try:
            self.store.save(self.state)
            self.durable_state = copy.deepcopy(self.state)
        except Exception:
            self.state = copy.deepcopy(self.durable_state)
            self.state["last_error_code"] = "storage_unavailable"
            fail("storage_unavailable")

    def set_budget(self, principal, values):
        with self.lock:
            self.authorize(principal, "manage")
            if not isinstance(values, list) or len(values) != 7 or not all(type(v) is int and v >= 0 for v in values):
                fail("seven_integer_budgets_required")
            if values != self.state["budgets"]:
                self.state["budgets"] = values[:]
                self.state["config_revision"] += 1
                self.state["previews"] = {}
                self.save()
            return {"budgets": values[:], "config_revision": self.state["config_revision"]}

    def set_metadata(self, principal, task_id, weight, area, contains_vacuuming,
                     fixed_date=None, advance_days=0):
        with self.lock:
            self.authorize(principal, "manage")
            if task_id not in self.state["source"]:
                fail("unknown_task")
            if weight is not None and not integer(weight, 1, 3):
                fail("invalid_weight")
            if contains_vacuuming is not None and type(contains_vacuuming) is not bool:
                fail("vacuum_classification_required")
            text(area)
            if not integer(advance_days, 0, 30):
                fail("invalid_advance_window")
            if fixed_date is not None:
                try:
                    datetime.strptime(fixed_date, "%Y-%m-%d")
                except (TypeError, ValueError):
                    fail("invalid_fixed_date")
            meta = dict(weight=weight, area=area, contains_vacuuming=contains_vacuuming,
                        fixed_date=fixed_date, advance_days=advance_days)
            if self.state["metadata"].get(task_id) != meta:
                self.state["metadata"][task_id] = meta
                if contains_vacuuming is True:
                    for occurrence in self.state["occurrences"].values():
                        if occurrence["task_id"] == task_id and occurrence["status"] != "completed" and not occurrence.get("vacuum_seen", False):
                            occurrence["vacuum_seen"] = True
                            self._touch_plans(occurrence["occurrence_id"])
                self.state["config_revision"] += 1
                self.state["previews"] = {}
                self.save()
            return copy.deepcopy(meta)

    def _fresh(self):
        if hasattr(self.store, "healthcheck"):
            self.store.healthcheck()
        last = self.state["last_sync"]
        if not last or self.state["last_error_code"]:
            fail("source_unavailable")
        if (self.now() - datetime.fromisoformat(last)).total_seconds() > 900:
            fail("source_stale")

    def sync(self, principal):
        with self.lock:
            self.authorize(principal, "sync")
            try:
                tasks, history = self.source.read()
                self._accept_source(tasks, history)
            except Exception:
                self.state["last_error_code"] = "source_sync_failed"
                self.save()
                fail("source_sync_failed")
            return {"last_sync": self.state["last_sync"], "source_revision": self.state["source_revision"]}

    def _accept_source(self, tasks, histories):
        # Validate the WHOLE read before committing; no silent truncation.
        if not isinstance(tasks, list) or len(tasks) > MAX_TASKS:
            fail("source_task_limit")
        if not isinstance(histories, dict):
            fail("invalid_history")
        task_map, history_map = {}, {}
        count = 0
        for raw in tasks:
            t = copy.deepcopy(raw)
            tid = text(t["task_id"], 80)
            if tid in task_map or type(t["active"]) is not bool:
                fail("invalid_source_task")
            text(t["title"])
            t["notes"] = notes(t.get("notes", ""))
            if t["due_at"] is not None:
                t["due_at"] = stamp(t["due_at"])
            if not integer(t["priority"], 0, 5):
                fail("invalid_priority")
            if not integer(t["sync_version"], 0, 2**63-1):
                fail("invalid_source_version")
            task_map[tid] = t
            seen, hs = set(), []
            for raw_h in histories.get(tid, []):
                h = copy.deepcopy(raw_h)
                hid = text(h["history_id"], 80)
                if hid in seen or h["task_id"] != tid:
                    fail("invalid_history")
                seen.add(hid)
                if h["due_at"] is not None:
                    h["due_at"] = stamp(h["due_at"])
                if h["completed_at"] is not None:
                    h["completed_at"] = stamp(h["completed_at"])
                if h["status"] == "completed" and not h["completed_at"]:
                    fail("invalid_completed_history")
                if h["status"] not in ("completed", "skipped", "other"):
                    fail("invalid_history_status")
                hs.append(h)
            count += len(hs)
            if count > MAX_HISTORY:
                fail("source_history_limit")
            history_map[tid] = hs
        if set(histories) != set(task_map):
            fail("incomplete_history")
        revision = digest([task_map, history_map])
        for request_key, req in self.state["requests"].items():
            payload = req.get("payload", {})
            resets = payload.get("floor_resets", [])
            mop = self.state["occurrences"].get(payload.get("occurrence_id"))
            if payload.get("floor_reset_confirmed") or not resets or not mop or req["phase"] not in ("writing", "reconciliation_required", "done"):
                continue
            matches = [h for h in history_map.get(mop["task_id"], [])
                       if h["history_id"] not in mop["history_baseline"] and h["due_at"] == mop["due_at"] and h["status"] == "completed"
                       and h.get("completion_request_key") == request_key]
            if len(matches) != 1:
                continue
            for reset in resets:
                current = task_map.get(reset["task_id"])
                if not current or current["sync_version"] <= reset["sync_version"]:
                    continue
                cleaned = datetime.fromisoformat(matches[0]["completed_at"]).astimezone(TZ).date().isoformat()
                self.state.setdefault("last_cleaning", {})[reset["task_id"]] = max(cleaned, self.state.get("last_cleaning", {}).get(reset["task_id"], cleaned))
                self.state.setdefault("floor_reset_generation", {})[reset["task_id"]] = request_key
                for prior in self.state["occurrences"].values():
                    if prior["task_id"] == reset["task_id"] and prior["due_at"] == reset["due_at"] and prior["status"] not in ("completed", "superseded") and prior.get("floor_reset_generation") != request_key:
                        prior.update(status="superseded", sync_status="confirmed", floor_reset_by=mop["occurrence_id"])
                        self._touch_plans(prior["occurrence_id"])
            payload["floor_reset_confirmed"] = True
        # A due change without history confirmation leaves an old occurrence unresolved.
        for occ in self.state["occurrences"].values():
            hs = history_map.get(occ["task_id"])
            if hs is None:
                continue
            matches = [h for h in hs if h["history_id"] not in occ["history_baseline"]
                       and h["due_at"] == occ["due_at"] and h["status"] == "completed"]
            old = (occ["status"], occ["completed_at"], occ["sync_status"])
            if len(matches) == 1:
                occ.update(status="completed", completed_at=matches[0]["completed_at"],
                           sync_status="confirmed", history_id=matches[0]["history_id"])
            elif len(matches) > 1:
                occ["sync_status"] = "reconciliation_required"
            elif occ.get("history_id"):
                # Actual source correction, never a local fake undo.
                occ.update(status="needs_action", completed_at=None,
                           sync_status="reconciliation_required", history_id=None)
            elif occ["sync_status"] != "pending_cancelable" and occ["status"] not in ("completed", "superseded"):
                current = task_map.get(occ["task_id"])
                if not current or current["due_at"] != occ["due_at"] or not current["active"]:
                    occ["sync_status"] = "reconciliation_required"
            if old != (occ["status"], occ["completed_at"], occ["sync_status"]):
                self._touch_plans(occ["occurrence_id"])
        self.state.update(source=task_map, history=history_map, source_revision=revision,
                          last_sync=self.now().isoformat(), last_error_code=None)
        self.save()

    def _touch_plans(self, oid):
        for plan in self.state["plans"].values():
            if any(e["occurrence_id"] == oid for e in plan["entries"]):
                plan["revision"] += 1

    def _current_occurrences(self):
        result = []
        for tid, task in sorted(self.state["source"].items()):
            if not task["active"] or not task["due_at"]:
                continue
            # Only completed/skipped history changes recurrence generation, never polling.
            baseline = sorted(h["history_id"] for h in self.state["history"].get(tid, [])
                              if h["status"] in ("completed", "skipped"))
            identity = [self.household_id, tid, task["due_at"], baseline]
            generation = self.state.get("floor_reset_generation", {}).get(tid)
            if generation:
                identity.append(generation)
            oid = digest(identity)[:32]
            occ = self.state["occurrences"].get(oid)
            if occ is None:
                occ = dict(task_id=tid, occurrence_id=oid, due_at=task["due_at"],
                           history_baseline=baseline, status="needs_action", sync_status="synced",
                           floor_reset_generation=generation,
                           completed_at=None, history_id=None,
                           vacuum_seen=self.state["metadata"].get(tid, {}).get("contains_vacuuming") is True)
                self.state["occurrences"][oid] = occ
            # A corrected completion must not invent a duplicate open identity.
            unresolved = [o for o in self.state["occurrences"].values()
                          if o["task_id"] == tid
                          and o["occurrence_id"] != oid and o["sync_status"] in
                          ("reconciliation_required", "writing", "pending_cancelable")]
            if unresolved:
                continue
            result.append(occ)
        return result

    def _forecast(self, day, budget, preserved=()):
        unresolved_tasks = {o["task_id"] for o in self.state["occurrences"].values()
                            if o["status"] != "completed" and o["sync_status"] == "reconciliation_required"
                            and self.state["source"].get(o["task_id"], {}).get("active") is not False}
        conflicts = [{"code": "occurrence_unresolved", "task_id": tid} for tid in sorted(unresolved_tasks)]
        candidates = []
        chosen = {e["occurrence_id"] for e in preserved}
        for occ in self._current_occurrences():
            tid = occ["task_id"]
            if occ["occurrence_id"] in chosen or occ["status"] in ("completed", "superseded"):
                continue
            t, m = self.state["source"][tid], self.state["metadata"].get(tid)
            due = datetime.fromisoformat(occ["due_at"]).astimezone(TZ)
            if not m or m["weight"] is None or m["contains_vacuuming"] is None:
                if due.date() <= day:
                    conflicts.append({"code": "metadata_required", "task_id": tid})
                continue
            if occ["sync_status"] != "synced":
                if tid not in unresolved_tasks:
                    conflicts.append({"code": "occurrence_unresolved", "task_id": tid})
                continue
            kind = canonical_type(t, m)
            vacuum = kind == "vacuum" or m["contains_vacuuming"] or occ.get("vacuum_seen", False)
            fixed = m["fixed_date"] is not None
            if vacuum and day.weekday() == NO_VACUUM_WEEKDAY and fixed and m["fixed_date"] <= day.isoformat():
                conflicts.append({"code": "sunday_fixed_vacuum", "task_id": tid})
            entry = dict(task_id=tid, occurrence_id=occ["occurrence_id"], title=t["title"],
                         area=m["area"], weight=m["weight"], contains_vacuuming=vacuum,
                         canonical_task_type=kind, due_at=occ["due_at"], due_date=None,
                         _due_day=due.date(), _fixed=fixed, _projected=False)
            following_due = self._following_due(t, due)
            entry['_smooth_eligible'] = not fixed and (t.get("frequency_type") == "once" or (following_due is not None and (following_due.date() - due.date()).days > 3))
            if due.date() <= day + timedelta(days=6):
                candidates.append(entry)
            # Forecast distinct regular occurrences; only the current source occurrence
            # is executable. Future projections never enter the completion journal.
            cursor = max(due, datetime.combine(day, due.timetz()))
            for _ in range(7):
                following = self._following_due(t, cursor)
                if following is None or following <= cursor or following.date() > day + timedelta(days=6):
                    break
                cursor = following
                if cursor.date() < day:
                    continue
                projected = dict(entry, due_at=cursor.astimezone(timezone.utc).isoformat(),
                                 _due_day=cursor.date(), _projected=True)
                projected["occurrence_id"] = digest([self.household_id, tid, projected["due_at"], occ["history_baseline"]])[:32]
                candidates.append(projected)
        candidates.sort(key=lambda e: (not e["_fixed"], e["_due_day"],
                                        self.state["source"][e["task_id"]]["priority"] or 6,
                                        e["task_id"], e["occurrence_id"]))
        kept = []
        for e in preserved:
            value = copy.deepcopy(e)
            value["canonical_task_type"] = canonical_type(self.state["source"].get(e["task_id"], e),
                                                         self.state["metadata"].get(e["task_id"]))
            kept.append(value)
        budgets = [self.state["budgets"][(day + timedelta(days=i)).weekday()] for i in range(7)]
        budgets[0] = budget
        result, unplaced = schedule(day, budgets, candidates, kept, smoothing=getattr(self, "gentle_smoothing", True))
        if sum(e["weight"] for e in kept) > budget:
            conflicts.append({"code": "completed_exceeds_budget", "points": sum(e["weight"] for e in kept), "budget": budget})
        today_ids = {e["occurrence_id"] for e in result[0]}
        for e in unplaced:
            if e["_due_day"] <= day and e["occurrence_id"] not in today_ids:
                # Sunday and room exclusions are rules, not capacity failures.
                if e["contains_vacuuming"] and day.weekday() == NO_VACUUM_WEEKDAY:
                    continue
                if e["canonical_task_type"] == "vacuum" and any(room(e) and room(e) == room(x) and x.get("canonical_task_type") == "mop" for x in result[0]):
                    continue
                conflicts.append({"code": "fixed_capacity" if e["_fixed"] else "capacity",
                                  "task_id": e["task_id"], "weight": e["weight"], "budget": budget})
        return result, conflicts

    @staticmethod
    def _following_due(task, due):
        kind = task.get("frequency_type")
        frequency = task.get("frequency", 1)
        if kind == "days_of_the_week":
            metadata = task.get("frequency_metadata") or {}
            if metadata.get("weekPattern") not in (None, "", "every_week"):
                return None
            try:
                local = due.astimezone(ZoneInfo(metadata.get("timezone") or "UTC"))
            except (ValueError, KeyError):
                return None
            weekdays = {str(v).casefold() for v in metadata.get("days", [])}
            for offset in range(1, 8):
                following = local + timedelta(days=offset)
                if following.strftime("%A").casefold() in weekdays:
                    return following.astimezone(TZ)
            return None
        if kind in ("daily", "weekly"):
            return due + timedelta(days=(frequency if integer(frequency, 1, 365) else 1) * (1 if kind == "daily" else 7))
        if kind == "interval":
            unit = (task.get("frequency_metadata") or {}).get("unit")
            if unit in ("days", "weeks") and integer(frequency, 1, 365):
                return due + timedelta(days=frequency * (7 if unit == "weeks" else 1))
            if unit in ("months", "years"):
                kind = "monthly" if unit == "months" else "yearly"
            else:
                return None
        else:
            frequency = frequency if integer(frequency, 1, 365) else 1
        if kind in ("monthly", "yearly") and integer(frequency, 1, 365):
            index = due.year * 12 + due.month - 1 + frequency * (12 if kind == "yearly" else 1)
            year, month = index // 12, index % 12 + 1
            # Match Go AddDate's calendar overflow, used by the source scheduler.
            return due.replace(year=year, month=month, day=1) + timedelta(days=due.day-1)
        return None

    def _select(self, day, budget, preserved=()):
        result, conflicts = self._forecast(day, budget, preserved)
        entries = [{k: v for k, v in e.items() if not k.startswith("_")}
                   for e in result[0] if not e.get("_projected")]
        return entries, conflicts

    def generate(self, principal):
        with self.lock:
            self.authorize(principal, "generate")
            day = self.now().date()
            if day.isoformat() in self.state["plans"]:
                return self.get_today(principal)
            self._fresh()
            if self.state["budgets"] is None:
                fail("budget_setup_required")
            budget = self.state["budgets"][day.weekday()]
            entries, conflicts = self._select(day, budget)
            self.state["plans"][day.isoformat()] = dict(
                plan_id=uuid.uuid4().hex, date=day.isoformat(), budget=budget, revision=1,
                config_revision=self.state["config_revision"], rules_version="gentle-smoothing-v1",
                entries=entries, conflicts=conflicts, generated_at=self.now().isoformat())
            self._prune()
            self.save()
            return self.get_today(principal)

    def _prune(self):
        # 90 local days; never evict an unresolved occurrence/request.
        cutoff = (self.now().date() - timedelta(days=90)).isoformat()
        self.state["plans"] = {d: p for d, p in self.state["plans"].items() if d >= cutoff}
        referenced = {e["occurrence_id"] for p in self.state["plans"].values() for e in p["entries"]}
        self.state["occurrences"] = {k: o for k, o in self.state["occurrences"].items()
                                     if k in referenced or o["status"] != "completed"}
        self.state["requests"] = {k: r for k, r in self.state["requests"].items()
                                  if r["created_date"] >= cutoff or r.get("phase") not in ("done", "canceled")}
        if len(self.state["requests"]) >= 5000 or len(self.state["occurrences"]) >= 10000:
            fail("journal_limit")

    def get_today(self, principal):
        with self.lock:
            self.authorize(principal, "read")
            now = self.now()
            day = now.date().isoformat()
            p = self.state["plans"].get(day)
            tasks, conflicts = [], copy.deepcopy(p["conflicts"] if p else [])
            conflicts = [c for c in conflicts if not (c.get("code") == "occurrence_unresolved"
                         and self.state["source"].get(c.get("task_id"), {}).get("active") is False)]
            if p:
                for entry in p["entries"]:
                    occ = self.state["occurrences"][entry["occurrence_id"]]
                    if occ["status"] == "superseded":
                        continue
                    meta = self.state["metadata"].get(entry["task_id"], {})
                    kind = canonical_type(entry, entry) if occ["status"] == "completed" else canonical_type(self.state["source"].get(entry["task_id"], entry), meta)
                    if kind == "vacuum" and occ["status"] != "completed" and any(
                        canonical_type(self.state["source"].get(other["task_id"], other), self.state["metadata"].get(other["task_id"], {})) == "mop"
                        and room(meta) and room(meta) == room(self.state["metadata"].get(other["task_id"], {}))
                        for other in p["entries"]):
                        continue
                    row = copy.deepcopy(entry)
                    row.update(status=occ["status"], sync_status=occ["sync_status"], completed_at=occ["completed_at"])
                    # New classification blocks immediately without rewriting history snapshots.
                    m = self.state["metadata"].get(entry["task_id"], {})
                    vacuum = kind == "vacuum" or entry["contains_vacuuming"] or occ.get("vacuum_seen", False)
                    if occ["status"] != "completed":
                        vacuum = vacuum or m.get("contains_vacuuming") is True
                    row["contains_vacuuming"] = vacuum
                    row["canonical_task_type"] = kind
                    row["notes"] = self.state["source"].get(entry["task_id"], {}).get("notes", "")
                    if now.weekday() == NO_VACUUM_WEEKDAY and vacuum and occ["status"] != "completed":
                        row["sync_status"] = "rule_blocked"
                        conflicts.append({"code": "sunday_vacuum_blocked", "task_id": entry["task_id"]})
                    if vacuum and occ["status"] == "completed" and occ["completed_at"] and \
                            datetime.fromisoformat(occ["completed_at"]).astimezone(TZ).weekday() == NO_VACUUM_WEEKDAY:
                        conflicts.append({"code": "external_sunday_vacuum", "task_id": entry["task_id"]})
                    tasks.append(row)
            completed = [t for t in tasks if t["status"] == "completed"]
            budget = p["budget"] if p else None
            points = sum(t["weight"] for t in tasks)
            cp = sum(t["weight"] for t in completed)
            storage_error = False
            if hasattr(self.store, "healthcheck"):
                try:
                    self.store.healthcheck()
                except Exception:
                    storage_error = True
            stale = storage_error or not self.state["last_sync"] or (now - datetime.fromisoformat(self.state["last_sync"])).total_seconds() > 900
            source_write_unverified = not bool(getattr(self.source, "write_verified", False))
            pending = any(t["sync_status"] in ("writing", "pending_cancelable", "reconciliation_required") for t in tasks)
            status = ("setup_required" if self.state["budgets"] is None else
                      "stale" if stale or self.state["last_error_code"] else
                      "plan_missing" if p is None else "error" if source_write_unverified else "sync_pending" if pending else
                      "conflict" if conflicts else "ready")
            allowed = self.permissions.get(principal, set())
            writes = bool(getattr(self.source, "write_verified", False))
            monday = now.date() - timedelta(days=now.weekday())
            weekly = {(tid, h["history_id"]) for tid, hs in self.state["history"].items() for h in hs
                      if h["status"] == "completed" and h["completed_at"] and
                      monday <= datetime.fromisoformat(h["completed_at"]).astimezone(TZ).date() <= now.date()}
            return dict(api_version=API_VERSION, household_id=self.household_id, plan_id=p["plan_id"] if p else None,
                        plan_revision=p["revision"] if p else None, date=day, timezone=str(TZ),
                        planner_state=status, last_sync=self.state["last_sync"],
                        last_plan_generation=p["generated_at"] if p else None,
                        last_error_code=("storage_unavailable" if storage_error else self.state["last_error_code"] or
                                         ("source_write_unverified" if p and source_write_unverified else None)),
                        week_budgets=copy.deepcopy(self.state["budgets"]),
                        capabilities={"read": "read" in allowed, "complete": "complete" in allowed and writes,
                                      "replan": "replan" in allowed, "reopen": False,
                                      "manage": "manage" in allowed, "task_management": "manage" in allowed and writes,
                                      "generate": "generate" in allowed, "sync": "sync" in allowed, "cancel_pending": "complete" in allowed and writes},
                        summary=dict(budget_today=budget, planned_points=points, completed_points=cp,
                                     open_points=points-cp, budget_remaining=budget-points if budget is not None else None,
                                     total_tasks=len(tasks), completed_tasks=len(completed), open_tasks=len(tasks)-len(completed),
                                     vacuum_blocked_today=now.weekday()==NO_VACUUM_WEEKDAY, week_completed_tasks=len(weekly)),
                        tasks=tasks, conflicts=conflicts)

    def _plan(self, plan_id, revision):
        p = self.state["plans"].get(self.now().date().isoformat())
        if not p or p["plan_id"] != plan_id:
            fail("not_today_plan")
        if type(revision) is not int or p["revision"] != revision:
            fail("stale_revision")
        return p

    def _request(self, principal, request_id, payload):
        text(request_id, 128)
        key = digest([principal, request_id])
        fp = digest(payload)
        old = self.state["requests"].get(key)
        if old and old["fingerprint"] != fp:
            fail("request_id_reused")
        if old:
            return key, old
        self._prune()
        return key, dict(fingerprint=fp, payload=payload, phase="new", created_date=self.now().date().isoformat(), principal=principal)

    def complete(self, principal, plan_id, occurrence_id, revision, request_id):
        with self.lock:
            self.authorize(principal, "complete")
            payload = dict(action="complete", plan_id=plan_id, occurrence_id=occurrence_id, revision=revision)
            key, request = self._request(principal, request_id, payload)
            if request["phase"] != "new":
                occ = self.state["occurrences"].get(occurrence_id)
                return {"result": "already_completed" if occ and occ["status"] == "completed" else request["phase"], "request_id": request_id}
            if not getattr(self.source, "write_verified", False):
                fail("source_write_unverified")
            # Confirmed occurrence retries before stale revision, but only for the named plan.
            old_p = next((p for p in self.state["plans"].values() if p["plan_id"] == plan_id), None)
            if old_p and any(e["occurrence_id"] == occurrence_id for e in old_p["entries"]):
                if self.state["occurrences"][occurrence_id]["status"] == "completed":
                    request.update(phase="done")
                    self.state["requests"][key] = request
                    self.save()
                    return {"result": "already_completed", "request_id": request_id}
            p = self._plan(plan_id, revision)
            entry = next((e for e in p["entries"] if e["occurrence_id"] == occurrence_id), None)
            if not entry:
                fail("occurrence_not_in_plan")
            self._write_guard(entry)
            occ = self.state["occurrences"][occurrence_id]
            if occ["sync_status"] != "synced":
                fail("reconciliation_required")
            request.update(phase="pending_cancelable", not_before=(self.now().astimezone(timezone.utc)+timedelta(seconds=5)).isoformat())
            self.state["requests"][key] = request
            occ["sync_status"] = "pending_cancelable"
            self._touch_plans(occurrence_id)
            self.save()
            return {"result": "pending_cancelable", "request_id": request_id, "not_before": request["not_before"]}

    def complete_extra(self, principal, task_id, sync_version, request_id):
        """Complete the real current occurrence, including early work outside a plan."""
        with self.lock:
            self.authorize(principal, "complete")
            key, request = self._request(principal, request_id,
                                        dict(action="complete_extra", task_id=task_id, sync_version=sync_version))
            if request["phase"] != "new":
                return {"result": request["phase"], "request_id": request_id}
            if not getattr(self.source, "write_verified", False):
                fail("source_write_unverified")
            self._fresh()
            current = self.state["source"].get(task_id)
            if not integer(sync_version, 1, 2**63-1) or not current or current["sync_version"] != sync_version:
                fail("task_edit_conflict")
            if any(h["status"] == "completed" and datetime.fromisoformat(h["completed_at"]).astimezone(TZ).date() == self.now().date()
                   for h in self.state["history"].get(task_id, [])):
                return {"result": "already_completed", "request_id": request_id}
            occ = next((o for o in self._current_occurrences() if o["task_id"] == task_id), None)
            if not occ or occ["sync_status"] != "synced":
                fail("reconciliation_required")
            meta = self.state["metadata"].get(task_id, {})
            entry = dict(task_id=task_id, occurrence_id=occ["occurrence_id"], due_at=occ["due_at"],
                         contains_vacuuming=meta.get("contains_vacuuming") is True)
            self._write_guard(entry)
            request["payload"].update(occurrence_id=occ["occurrence_id"], entry=entry,
                                      completion_date=self.now().date().isoformat())
            request.update(phase="pending_cancelable", not_before=(self.now().astimezone(timezone.utc)+timedelta(seconds=5)).isoformat())
            self.state["requests"][key] = request
            occ["sync_status"] = "pending_cancelable"
            self._touch_plans(occ["occurrence_id"])
            self.save()
            return {"result": "pending_cancelable", "request_id": request_id, "not_before": request["not_before"]}

    def _floor_resets(self, entry):
        task = self.state["source"].get(entry["task_id"], {})
        meta = self.state["metadata"].get(entry["task_id"], {})
        if canonical_type(task, meta) != "mop" or not room(meta):
            return []
        result = []
        for tid, related in sorted(self.state["source"].items()):
            related_meta = self.state["metadata"].get(tid, {})
            if related["active"] and room(related_meta) == room(meta) and canonical_type(related, related_meta) == "vacuum":
                if any(o["task_id"] == tid and o["sync_status"] in ("writing", "pending_cancelable", "reconciliation_required")
                       for o in self.state["occurrences"].values() if o["status"] != "completed"):
                    fail("floor_reset_pending")
                result.append(dict(task_id=tid, sync_version=related["sync_version"], due_at=related["due_at"]))
        return result

    def _write_guard(self, entry):
        self._fresh()
        tid = entry["task_id"]
        current = self.state["source"].get(tid)
        meta = self.state["metadata"].get(tid)
        if not current or not current["active"] or not meta or meta["contains_vacuuming"] is None:
            fail("task_not_writable")
        if self.now().weekday() == NO_VACUUM_WEEKDAY and (canonical_type(current, meta) == "vacuum" or entry["contains_vacuuming"] or meta["contains_vacuuming"] or
                                       self.state["occurrences"][entry["occurrence_id"]].get("vacuum_seen", False)):
            fail("sunday_vacuum_forbidden")
        if canonical_type(current, meta) == "vacuum" and room(meta):
            today = self.now().date().isoformat()
            plan = self.state["plans"].get(today, {})
            mops = {tid for tid, t in self.state["source"].items()
                    if canonical_type(t, self.state["metadata"].get(tid)) == "mop"
                    and room(meta) == room(self.state["metadata"].get(tid, {}))}
            if any(e["task_id"] in mops for e in plan.get("entries", [])) or any(
                h["status"] == "completed" and datetime.fromisoformat(h["completed_at"]).astimezone(TZ).date().isoformat() == today
                for tid in mops for h in self.state["history"].get(tid, [])):
                fail("room_mop_priority")
        if current["due_at"] != entry["due_at"]:
            fail("occurrence_changed")

    def cancel_pending(self, principal, request_id):
        with self.lock:
            self.authorize(principal, "complete")
            key = digest([principal, text(request_id, 128)])
            r = self.state["requests"].get(key)
            if not r or r["phase"] != "pending_cancelable":
                fail("not_cancelable")
            r["phase"] = "canceled"
            oid = r["payload"]["occurrence_id"]
            self.state["occurrences"][oid]["sync_status"] = "synced"
            self._touch_plans(oid)
            self.save()
            return {"result": "canceled"}

    def flush_pending(self, principal):
        """Single scheduler path. Persist 'writing' BEFORE I/O; never retry uncertain POST."""
        with self.lock:
            self.authorize(principal, "complete")
            if not getattr(self.source, "write_verified", False):
                return
            self.sync(principal)  # reconcile externally completed requests first
            for key, r in list(self.state["requests"].items()):
                if r["phase"] != "pending_cancelable" or self.now() < datetime.fromisoformat(r["not_before"]):
                    continue
                payload = r["payload"]
                oid = payload["occurrence_id"]
                occ = self.state["occurrences"][oid]
                if occ["status"] == "completed":
                    r["phase"] = "done"
                    continue
                p = self.state["plans"].get(self.now().date().isoformat())
                extra = payload["action"] == "complete_extra"
                if extra:
                    entry = payload["entry"]
                    p = {"date": payload["completion_date"]}
                else:
                    entry = next((e for e in p["entries"] if e["occurrence_id"] == oid), None) if p and p["plan_id"] == payload["plan_id"] else None
                try:
                    if not entry or p["date"] != self.now().date().isoformat():
                        fail("not_today_plan")
                    if extra and self.state["source"].get(occ["task_id"], {}).get("sync_version") != payload["sync_version"]:
                        fail("task_edit_conflict")
                    self._write_guard(entry)
                    resets = self._floor_resets(entry)
                    if resets and not hasattr(self.source, "complete_floor"):
                        fail("source_floor_reset_unverified")
                    payload["floor_resets"] = resets
                    self.authorize(principal, "complete")
                    self.authorize(r["principal"], "complete")
                except HouseholdError:
                    r["phase"] = "blocked"
                    occ["sync_status"] = "reconciliation_required"
                    self._touch_plans(oid)
                    self.save()
                    continue
                r["phase"] = "writing"
                occ["sync_status"] = "writing"
                self.save()
                try:
                    args = (occ["task_id"], occ["due_at"], self.state["source"][occ["task_id"]]["sync_version"], key,
                            lambda: self._final_floor_guard(entry, p, r["principal"], resets))
                    if resets:
                        self.source.complete_floor(*args, resets)
                    else:
                        self.source.complete(*args)
                except Exception:
                    pass  # even 5xx may follow a committed write; history is authoritative
                r["phase"] = "reconciliation_required"
                occ["sync_status"] = "reconciliation_required"
                self._touch_plans(oid)
                self.save()
                try:
                    self.sync(principal)
                except HouseholdError:
                    continue
                if occ["status"] == "completed":
                    r["phase"] = "done"
                self.save()
            self.save()

    def _final_floor_guard(self, entry, plan, principal, resets):
        self._final_guard(entry, plan, principal)
        if resets != self._floor_resets(entry):
            fail("floor_reset_changed")

    def _final_guard(self, entry, plan, principal):
        self.authorize(principal, "complete")
        if self.write_principal_check is not None:
            self.write_principal_check(principal)
        if plan["date"] != self.now().date().isoformat():
            fail("not_today_plan")
        self._write_guard(entry)

    def replan(self, principal, plan_id, revision, dry_run=True, preview_id=None, request_id=None):
        with self.lock:
            self.authorize(principal, "replan")
            if type(dry_run) is not bool:
                fail("invalid_dry_run")
            if not dry_run:
                payload = dict(action="replan", plan_id=plan_id, revision=revision, preview_id=preview_id)
                key, req = self._request(principal, request_id, payload)
                if req["phase"] == "done":
                    return copy.deepcopy(req["result"])
            if not dry_run:
                self.sync(principal)  # revalidate source at commit, not from cached preview
            p = self._plan(plan_id, revision)
            self._fresh()
            if self.state["budgets"] is None:
                fail("budget_setup_required")
            if any(self.state["occurrences"][e["occurrence_id"]]["sync_status"] in
                   ("writing", "pending_cancelable", "reconciliation_required") for e in p["entries"]):
                fail("pending_actions")
            budget = self.state["budgets"][self.now().weekday()]
            preserved = [e for e in p["entries"] if self.state["occurrences"][e["occurrence_id"]]["status"] == "completed"]
            entries, conflicts = self._select(self.now().date(), budget, preserved)
            fingerprint = digest([self.now().date().isoformat(), p, self.state["source_revision"],
                                  self.state["config_revision"], entries, conflicts, budget])
            if dry_run:
                pid = uuid.uuid4().hex
                before = {e["occurrence_id"] for e in p["entries"]}
                after = {e["occurrence_id"] for e in entries}
                preview = dict(preview_id=pid, fingerprint=fingerprint, entries=entries, conflicts=conflicts,
                               budget=budget, added=sorted(after-before), removed=sorted(before-after),
                               planned_points=sum(e["weight"] for e in entries),
                               expires_at=(self.now().astimezone(timezone.utc)+timedelta(minutes=5)).isoformat())
                # Only one bounded active preview; persisted separately, active plan untouched.
                self.state["previews"] = {pid: preview}
                self.save()
                return {k: copy.deepcopy(v) for k, v in preview.items() if k != "fingerprint"}
            preview = self.state["previews"].get(preview_id)
            if not preview or preview["fingerprint"] != fingerprint or self.now() > datetime.fromisoformat(preview["expires_at"]):
                fail("stale_preview")
            p.update(entries=entries, conflicts=conflicts, budget=budget,
                     config_revision=self.state["config_revision"], revision=p["revision"]+1)
            self.state["previews"] = {}
            result = {"plan_id": p["plan_id"], "plan_revision": p["revision"], "result": "applied"}
            req.update(phase="done", result=result)
            self.state["requests"][key] = req
            self.save()
            return copy.deepcopy(result)

    def list_tasks(self, principal):
        with self.lock:
            self.authorize(principal, "manage")
            self._fresh()
            def last_cleaning(tid):
                dates = [datetime.fromisoformat(h["completed_at"]).astimezone(TZ).date().isoformat()
                         for h in self.state["history"].get(tid, []) if h["status"] == "completed"]
                imported = self.state.get("last_cleaning", {}).get(tid)
                if imported:
                    dates.append(imported)
                return max(dates) if dates else None
            occurrences = {o["task_id"]: o for o in self._current_occurrences()}
            return {"tasks": [dict(copy.deepcopy(t), last_cleaning=last_cleaning(tid),
                                  canonical_task_type=canonical_type(t, self.state["metadata"].get(tid)),
                                  occurrence_id=occurrences.get(tid, {}).get("occurrence_id"),
                                  completion_sync_status=occurrences.get(tid, {}).get("sync_status", "reconciliation_required"), metadata=copy.deepcopy(self.state["metadata"].get(tid)))
                              for tid, t in sorted(self.state["source"].items())],
                    "budgets": copy.deepcopy(self.state["budgets"]),
                    "config_revision": self.state["config_revision"]}

    def manage_task(self, principal, request_id, operation, fields=None, task_id=None):
        with self.lock:
            self.authorize(principal, "manage")
            if operation not in ("save", "archive"):
                fail("invalid_management_operation")
            key, req = self._request(principal, request_id, dict(action="manage_task", operation=operation,
                                                               fields=fields, task_id=task_id))
            if req["phase"] == "done":
                return copy.deepcopy(req["result"])
            if req["phase"] != "new":
                fail("management_reconciliation_required")
            if any(r.get("payload", {}).get("action") == "manage_task" and
                   r.get("phase") in ("writing", "reconciliation_required")
                   for r in self.state["requests"].values()):
                # A new request ID must not bypass an uncertain create/edit/archive.
                fail("management_reconciliation_required")
            if not getattr(self.source, "write_verified", False):
                fail("source_write_unverified")
            self._fresh()
            source_fields = copy.deepcopy(fields)
            last = None
            if operation == "save":
                if not isinstance(fields, dict):
                    fail("invalid_task_fields")
                if "description" in fields:
                    notes(fields["description"])
                last = source_fields.pop("lastCleanedDate", None)
                if last is not None:
                    source_fields["nextDueDate"] = next_cleaning(last, fields.get("frequencyType"), fields.get("frequency"))
                    if last > self.now().astimezone(TZ).date().isoformat():
                        fail("last_cleaning_in_future")
                if task_id:
                    expected = fields.get("syncVersion")
                    if not integer(expected, 1, 2**63-1):
                        fail("task_edit_revision_required")
                    if self.state["source"].get(task_id, {}).get("sync_version") != expected:
                        fail("task_edit_conflict")
            req["phase"] = "writing"
            self.state["requests"][key] = req
            self.save()
            try:
                actual = (self.source.save_task(source_fields, task_id) if operation == "save"
                          else self.source.archive_task(task_id))
            except HouseholdError as exc:
                if str(exc) in ("task_edit_conflict", "task_edit_revision_required"):
                    # Adapter rejected before sending a source mutation. This is not an uncertain write.
                    del self.state["requests"][key]
                    self.save()
                    raise
                req["phase"] = "reconciliation_required"
                self.save()
                fail("management_reconciliation_required")
            except Exception:
                req["phase"] = "reconciliation_required"
                self.save()
                fail("management_reconciliation_required")
            try:
                self.sync(principal)
            except Exception:
                req["phase"] = "reconciliation_required"
                self.save()
                fail("management_reconciliation_required")
            result = {"task_id": actual["task_id"], "result": "confirmed"}
            if last is not None:
                self.state.setdefault("last_cleaning", {})[actual["task_id"]] = last
            req.update(phase="done", result=result)
            self.save()
            return copy.deepcopy(result)
