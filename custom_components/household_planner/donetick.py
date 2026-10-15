"""Version-bound Donetick v0.1.79 internal API adapter (source-reviewed).

No HTML scraping, DB writes, external eAPI Plus endpoints or automatic retries.
write_verified defaults FALSE until an isolated installed-version contract is accepted.
"""
import ipaddress
import copy
import json
import re
import time
import stat
from pathlib import Path
from urllib.parse import urlsplit
import urllib.request

from .engine import MAX_TASKS, MAX_HISTORY, fail, stamp, integer, text, digest, notes

VERSION = "v0.1.79"
VERIFIED_BUILD = "v0.1.79-household-planner1"
LEGACY_BUILD = "v0.1.79-household-planner1"
SOURCE_COMMIT = "15bbf141eb2e17b577fa802ed0d12157f97fcf44"


def credential_provider(value):
    if not isinstance(value, str) or not value.strip() or value.startswith("@file:"):
        fail("source_credentials_unavailable")
    return lambda: value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Donetick:
    def __init__(self, base_url, secret_provider, expected_version=VERSION,
                 write_verified=False, transport=None, required_build=None):
        u = urlsplit(base_url)
        if u.scheme not in ("http", "https") or u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
            fail("invalid_source_url")
        try:
            host = ipaddress.ip_address(u.hostname)
        except ValueError:
            fail("source_private_ip_required")
        networks = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7")]
        if not any(host in n for n in networks):
            fail("source_private_ip_required")
        if expected_version != VERSION:
            fail("unsupported_donetick_version")
        self.base_url = base_url.rstrip("/")
        self.secret_provider = secret_provider
        self.required_build = required_build
        self.write_verified = write_verified is True
        self.transport = transport or self._http
        self.opener = urllib.request.build_opener(NoRedirect())

    def verify_runtime(self):
        if self.required_build is None:
            return
        try:
            health = self.transport("GET", "/health", None)
            if health.get("status") == "healthy" and health.get("version") == self.required_build and self.required_build in (VERIFIED_BUILD, LEGACY_BUILD):
                return
        except Exception:
            pass
        fail("source_build_unverified")

    def _http(self, method, path, body):
        token = self.secret_provider()
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            fail("source_credentials_unavailable")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url+path, data=data, method=method,
                                     headers={"secretkey": token, "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=5) as res:
                if res.status not in (200, 201):
                    fail("source_http_error")
                payload = res.read(2*1024*1024+1)
                if len(payload) > 2*1024*1024:
                    fail("source_payload_limit")
                return json.loads(payload)
        except Exception:
            fail("source_transport_error")

    @staticmethod
    def _id(task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", task_id):
            fail("invalid_task_id")
        return task_id

    def _call(self, method, path, body=None, allow_message=False):
        try:
            value = self.transport(method, path, body)
            if allow_message and isinstance(value, dict) and isinstance(value.get("message"), str):
                return None
            if not isinstance(value, dict) or "res" not in value:
                fail("source_contract_error")
            return value["res"]
        except Exception:
            fail("source_contract_error")

    @staticmethod
    def normalize(raw):
        if not isinstance(raw, dict) or not integer(raw.get("id"), 1, 2**63-1):
            fail("source_contract_error")
        if type(raw.get("isActive")) is not bool or not integer(raw.get("syncVersion"), 1, 2**63-1):
            fail("source_contract_error")
        due = raw.get("nextDueDate")
        return dict(task_id=str(raw["id"]), title=text(raw["name"]), active=raw["isActive"],
                    due_at=stamp(due) if due else None, priority=raw.get("priority", 0),
                    frequency_type=raw["frequencyType"], frequency=raw.get("frequency"),
                    frequency_metadata=copy.deepcopy(raw.get("frequencyMetadata") or {}), rolling=raw.get("isRolling", False), sync_version=raw["syncVersion"],
                    require_approval=raw.get("requireApproval", False), notes=notes("" if raw.get("description") is None else raw["description"]))

    def read(self):
        self.verify_runtime()
        started = time.monotonic()
        raw_tasks = self._call("GET", "/api/v1/chores/?includeArchived=true")
        if not isinstance(raw_tasks, list) or len(raw_tasks) > MAX_TASKS:
            fail("source_task_limit")
        tasks, history, count = [], {}, 0
        for raw in raw_tasks:
            if time.monotonic()-started > 30:
                fail("source_sync_time_limit")
            task = self.normalize(raw)
            tid = task["task_id"]
            hs = self._call("GET", f"/api/v1/chores/{self._id(tid)}/history")
            if not isinstance(hs, list):
                fail("source_contract_error")
            count += len(hs)
            if count > MAX_HISTORY:
                fail("source_history_limit")
            history[tid] = []
            for h in hs:
                if not integer(h.get("id"), 1, 2**63-1) or h.get("choreId") != int(tid):
                    fail("source_contract_error")
                history[tid].append(dict(history_id=str(h["id"]), task_id=tid,
                                         due_at=h.get("dueDate"), completed_at=h.get("performedAt"),
                                         status={1: "completed", 2: "skipped"}.get(h.get("status"), "other"),
                                         completion_request_key=(h["notes"][18:] if isinstance(h.get("notes"), str) and re.fullmatch(r"household-planner:[0-9a-f]{64}", h["notes"]) else None)))
            tasks.append(task)
        after = self._call("GET", "/api/v1/chores/?includeArchived=true")
        if not isinstance(after, list) or digest([self.normalize(t) for t in after]) != digest(tasks):
            fail("source_changed_during_read")
        return tasks, history

    def complete(self, task_id, due_at, sync_version, request_key, final_guard):
        return self.complete_floor(task_id, due_at, sync_version, request_key, final_guard, [])

    def complete_floor(self, task_id, due_at, sync_version, request_key, final_guard, floor_resets):
        if not self.write_verified:
            fail("source_write_unverified")
        self.verify_runtime()
        if floor_resets and self.required_build != VERIFIED_BUILD:
            fail("source_floor_reset_unverified")
        resets = []
        for reset in floor_resets:
            related = self.normalize(self.task_details(reset["task_id"]))
            if related["sync_version"] != reset["sync_version"] or not related["active"]:
                fail("source_occurrence_changed")
            resets.append({"id": int(self._id(reset["task_id"])), "syncVersion": reset["sync_version"]})
        # Adapter checks are advisory; the pinned source build performs the
        # primary/related version comparisons in one SQL transaction.
        current = self._call("GET", f"/api/v1/chores/{self._id(task_id)}")
        t = self.normalize(current)
        if t["due_at"] != due_at or t["sync_version"] != sync_version or not t["active"]:
            fail("source_occurrence_changed")
        if t["require_approval"]:
            fail("source_approval_required")
        final_guard()
        return self._call("POST", f"/api/v1/chores/{task_id}/do",
                          {"notes": "household-planner:"+request_key,
                           "actionOptions": {"syncVersion": sync_version, "nextDueDate": due_at},
                           "floorResets": resets})

    def task_details(self, task_id):
        return self._call("GET", f"/api/v1/chores/{self._id(task_id)}")

    def save_task(self, fields, task_id=None):
        """Only the reviewed minimal recurring-task subset; preserve unrelated metadata on edit."""
        if not self.write_verified:
            fail("source_write_unverified")
        self.verify_runtime()
        allowed = {"name", "frequencyType", "frequency", "nextDueDate", "isActive", "description", "syncVersion"}
        if not isinstance(fields, dict) or set(fields)-allowed:
            fail("invalid_task_fields")
        text(fields.get("name"))
        if "description" in fields:
            notes(fields["description"])
        if task_id and not integer(fields.get("syncVersion"), 1, 2**63-1):
            fail("task_edit_revision_required")
        if not task_id and "syncVersion" in fields:
            fail("unexpected_task_revision")
        if fields.get("frequencyType") not in ("once", "daily", "weekly", "monthly", "yearly", "interval", "no_repeat"):
            fail("unsupported_recurrence")
        if fields.get("frequencyType") not in ("once", "no_repeat") and not integer(fields.get("frequency"), 1, 365):
            fail("invalid_frequency")
        if type(fields.get("isActive")) is not bool:
            fail("invalid_active")
        if fields.get("nextDueDate") is not None:
            stamp(fields["nextDueDate"])
        if task_id:
            old = self.task_details(task_id)
            if old["syncVersion"] != fields["syncVersion"]:
                fail("task_edit_conflict")
            # PUT expects the full ChoreReq; copy only its verified fields.
            keys = {"id", "name", "frequencyType", "frequency", "frequencyMetadata", "nextDueDate", "isRolling", "assignedTo", "assignees", "assignStrategy", "isActive", "notification", "notificationMetadata", "labelsV2", "updatedAt", "priority", "completionWindow", "points", "description", "subTasks", "requireApproval", "isPrivate", "projectId"}
            body = {k: v for k, v in old.items() if k in keys}
            # The source CAS patch checks this snapshot at the SQL write boundary.
            # Stock v0.1.79 ignores the extra field; its write gate stays closed.
            body["syncVersion"] = old["syncVersion"]
            if body.get("labelsV2"):
                # Label response and LabelReq differ; cannot safely round-trip yet.
                fail("task_edit_labels_unverified")
            body.update(fields)
            self._call("PUT", "/api/v1/chores/", body, allow_message=True)
            tid = task_id
        else:
            body = dict(fields, assignStrategy="no_assignee", assignees=[], notification=False)
            tid = str(self._call("POST", "/api/v1/chores/", body))
            self._id(tid)
        actual = self.task_details(tid)
        if any(actual.get(k) != v for k, v in fields.items() if k not in ("nextDueDate", "syncVersion")):
            fail("source_write_readback_mismatch")
        if fields.get("nextDueDate") and stamp(actual.get("nextDueDate")) != stamp(fields["nextDueDate"]):
            fail("source_write_readback_mismatch")
        return self.normalize(actual)

    def archive_task(self, task_id):
        if not self.write_verified:
            fail("source_write_unverified")
        self.verify_runtime()
        self._call("PUT", f"/api/v1/chores/{self._id(task_id)}/archive", {}, allow_message=True)
        actual = self.task_details(task_id)
        if actual.get("isActive") is not False:
            fail("source_write_readback_mismatch")
        return self.normalize(actual)
