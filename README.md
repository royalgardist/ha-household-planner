# Household Planner for Home Assistant

A local chore planner with daily effort budgets, optional gentle workload smoothing,
completion tracking and a simple dashboard card. No AI, Jarvis, cloud account or subscription required.

**Early community beta (0.1.0). This is not a plug-in for stock Donetick.**
It currently requires the companion patched Donetick backend below. The scheduling core
is derived from a working private installation; the portable setup and card are new.
This is a custom repository, not an officially accepted Home Assistant/HACS integration.

## What it does

- Keeps a stable daily plan; completing tasks does not continually refill the day.
- Assigns effort weights 1–3 and separate budgets for each weekday.
- Offers **Gently distribute tasks** in integration options (off by default): move suitable
  real tasks at most two days earlier when this improves budget-relative balance. Fixed
  dates, unknown recurrence and chores recurring every three days or less are excluded.
  Donetick due dates are not edited by planning.
- Retains the original overload handling: overflow can move up to three days earlier,
  and overdue work can use later available capacity. Turning smoothing off disables the
  additional balancing pass, not this baseline overload handling.
- Offers **Keep Sundays free of vacuuming** (on by default).
- Avoids vacuuming and mopping the same room on the same day.
- Queues a completion for five seconds so it can be cancelled. Uses transactional version
  checks in the companion backend to avoid overwriting concurrent edits.
- Provides sensors, a read-only native to-do view and authenticated response services.
  Readers see plans, writers complete/replan, managers set budgets/task metadata.

## 1. Companion Donetick backend

**Do not point this at an unmodified production backend and assume writes work.**
The exact supported build is `v0.1.79-household-planner1`, based on upstream
[`15bbf141eb2e17b577fa802ed0d12157f97fcf44`](https://github.com/donetick/donetick/tree/15bbf141eb2e17b577fa802ed0d12157f97fcf44).
`donetick-cas2.patch` supplies the version-checked completion, metadata and
related floor-task updates used by the integration. All changes are included as source.

With Git and Docker installed, run from this repository:

```sh
sh build-backend.sh
```

This builds `household-planner-donetick:0.1.0` using the upstream Dockerfile. Deploy it
using [Donetick's self-hosting instructions](https://docs.donetick.com/getting-started/),
substituting this image for the upstream image. Keep your persistent database and
configuration mounts; do not replace/delete an existing database. Use a separate
instance for first evaluation. Set your own JWT secret and API key using Donetick;
no credentials are included here. Keep the backend on your private network.

The HA adapter accepts a private numeric IP URL with optional port (HTTP/HTTPS), no
URL credentials, path or query. It checks `/health` and the actual task API on setup.
Do not expose an unencrypted API port to the Internet. TLS certificate validation
remains enabled. Keep existing configuration/database copies before a version change.

## 2. Install the Home Assistant integration

Requires Home Assistant 2026.9 or newer. One household per HA installation.

**HACS custom repository:** HACS → menu → Custom repositories → add
`https://github.com/royalgardist/ha-household-planner`, category **Integration**.
Download Household Planner and restart Home Assistant.

**Manual:** copy `custom_components/household_planner` into your HA
`config/custom_components` directory, then restart HA.

Settings → Devices & services → Add integration → **Household Planner**.
Enter the backend URL, Donetick API key, and the manager's active HA administrator user ID.
User IDs are shown in Settings → People → Users (enable advanced mode if needed).
Optionally add comma-separated reader/writer user IDs. Choose daily budgets.

Credentials stay in HA's configuration store. State is stored under
`.storage/household_planner/<config-entry-id>/planner.sqlite3`; include this directory in
normal HA backups. This public version uses HA's configured storage, with no NAS mount requirement.

## 3. Dashboard and task setup

Add dashboard resource `/household_planner/card.js` as **JavaScript module**.
Add a manual card (the config entry ID is in the integration's detail-page URL):

```yaml
type: custom:household-planner-card
config_entry_id: YOUR_CONFIG_ENTRY_ID
title: Household
```

Open **Settings** in the card and assign effort, room and vacuum flag for each task.
Create chores and edit their recurrence in Donetick. Tasks with incomplete metadata are
not silently assigned guessed effort; the plan reports a conflict. Sync, then choose
**Preview new plan** and **Apply preview** to intentionally rebuild an existing plan.
Existing completions are preserved. Daily generation otherwise runs automatically.

Settings → Devices & services → Household Planner → **Configure** offers the optional
smoothing and Sunday switches with short explanations in English and German.
Changes affect future plans; they do not silently rewrite today's saved plan.

## Services and permissions

All services use domain `household_planner`, require `config_entry_id`, and return a
response. `services.yaml` lists the fields. Core services: `get_today`, `sync`,
`generate_today_plan`, `complete_task`, `cancel_pending`, `replan_today`,
`get_settings`, `set_week_budget`, `set_task_metadata`.

Mutations require a real authenticated HA user with the corresponding configured role.
Hiding a card does not grant or remove permissions. Unauthenticated/system automation
contexts cannot impersonate a user. The integration's internal scheduler syncs and
creates plans; it does not invent completions. Native to-do entities are read-only;
use the card for completion with revision and request IDs.

## Limits and verification

The adapter is intentionally pinned, with a maximum of 250 chores and 2,000 imported
history records per source read. It supports the recurrence forms implemented in the
adapter; unsupported source data produces a conflict instead of a guessed completion.
Calendar projections are estimates, not promises about future workload.

Run `python3 -m unittest test_planning`. Tests cover the balancing switch and key
scheduling exclusions using synthetic chores. Source patch application, Python syntax
and card JavaScript syntax are also checked during preparation. An independent fresh
community installation is **not yet claimed as verified**; please report issues with
versions and redacted error codes, never API keys or household exports.

## License and thanks

AGPL-3.0-or-later; see [LICENSE](LICENSE). The included backend patch modifies
[Donetick](https://github.com/donetick/donetick), also AGPL-3.0; its upstream authors
retain their copyrights. The build script retrieves the exact corresponding upstream
source and leaves it alongside the patch for inspection. Home Assistant and HACS are
separate projects; this is an independent community integration.
