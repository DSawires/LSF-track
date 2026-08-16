# CLAUDE.md

Project instructions for Claude Code. Read this before making changes.

## What this is

A production tracking system for a bespoke contract furniture manufacturer. Technical
office engineers work on the factory floor and log the movement of items through
production stages from their phones. The system answers three questions:

1. Where is every item right now?
2. Which stage is backed up, and by how much?
3. What has been sitting too long, and since when?

The users are a small number of engineers (single digits), each owning one or more
projects. The factory has patchy network coverage. Both facts drive most of the
design decisions below.

## Stack

- Backend: Python, FastAPI, SQLAlchemy, Alembic
- Database: PostgreSQL
- Frontend: PWA, mobile-first, service worker + IndexedDB write queue
- Auth: session-based, simple. No SSO, no multi-tenancy.

## Non-negotiable architecture rules

These are load-bearing. Do not work around them; if one seems to block a feature,
stop and raise it rather than designing around it.

### 1. The event log is append-only

`events` is the source of truth. Rows are inserted, never updated, never deleted.
Current state is **derived** from the log by query.

Do not add a `current_status`, `current_stage`, or `is_complete` column to `items`.
A materialized view or cached projection is acceptable **only** if it is fully
rebuildable from `events` and is rebuilt by a documented command. If a projection
and the log disagree, the log wins.

Corrections are new events (`event_type = 'correction'`) referencing the event they
supersede via `supersedes_event_id`. History is never rewritten.

### 2. Stages, stations and routes are data, not code

The factory will add stages. Assume this will happen repeatedly and that the person
adding one will not be a developer.

Adding a stage must require **zero code changes and zero deploys**. It is an insert
into `stages`, plus optionally a new route template version.

Concretely, this means:

- No Python `Enum` or TypeScript union type listing stage or station names.
- No string literals of stage codes anywhere outside seed data and tests.
- No `if stage.code == "paint"` or `match stage.code` branching in business logic.
- No report, query, or UI component that hardcodes a set of columns per stage.
  Aggregations iterate over whatever is in `stages`.
- No CHECK constraints enumerating stage codes.

Anything stage-specific must be expressed as a **flag or attribute on the stage row**
(e.g. `requires_external_po`, `allows_partial_qty`, `is_terminal`), never as a branch
on its name. If a new behaviour is needed, add a column to `stages`, give it a
default that preserves current behaviour, and branch on the column.

The same applies to `event_types` and `reason_codes`: table-driven, not enums.

### 3. Routes are versioned and snapshotted at release

`route_templates` are versioned. When an item is released to production, its steps
are **copied** into `item_steps`. Changing a template never alters the route of an
item already in production.

This is what makes adding a stage safe: new work picks up the new route, in-flight
work keeps the route it was released against.

Use sequence numbers in increments of 10 so a stage can be inserted between two
existing ones without renumbering.

### 4. Quantities live on events

An item is usually a batch (e.g. 120 identical bedside tables). Events carry a `qty`,
so a batch can partially advance. Never assume an item moves as an indivisible unit.

Quantity in a given state is derived by summing events. Guard against a batch
advancing more units than exist at the previous step, and surface that as a
validation error rather than silently allowing it.

### 5. Rework is forward motion, not a reversal

A piece returning from QC to an earlier stage is an event of type `rework_return`
with a `reason_code` and a target step, not a backwards transition or a deleted
event. Throughput and cycle-time reports must exclude rework quantities from
completed counts, or they will double-count.

### 6. Facts, not judgments

No `percent_complete` field. No `status` free-text. No "on track / at risk" flag.
Users record what happened: item, stage, station, state, quantity, time. Any
health assessment is computed from the log, never typed in.

## Data model

Names are indicative; Alembic migrations are authoritative.

- `projects` — client jobs
- `stages` — a step type (carpentry, veneer, paint, upholstery, outsourced, QC,
  packing, …). Carries behaviour flags. **Extensible at runtime.**
- `stations` — physical instances of a stage (paint_1, paint_2, carpentry_1, …).
  Many stations per stage. Recorded on events for load balancing.
- `route_templates` / `route_template_steps` — versioned standard routes per product
  family
- `items` — a batch: code, project, description, total qty, current drawing revision,
  route template + version, target release date
- `item_steps` — the snapshot of the route for this item
- `events` — the log. See below.
- `event_types`, `reason_codes` — table-driven vocabularies
- `users`

### `events` columns

| Column | Notes |
|---|---|
| `id` | UUID, **generated by the client** |
| `item_id` | |
| `item_step_id` | nullable for item-level events like revision bumps |
| `station_id` | nullable; required where the stage has stations |
| `event_type_id` | |
| `state` | queued / in_progress / completed — from a lookup table |
| `qty` | |
| `reason_code_id` | nullable |
| `occurred_at` | device time, when it happened on the floor |
| `received_at` | server time, set on insert |
| `user_id` | |
| `note` | nullable, free text, never parsed |
| `supersedes_event_id` | nullable, for corrections |

Index for the queries that matter: `(item_id, occurred_at)`,
`(station_id, occurred_at)`, `(received_at)`.

## Offline and sync

Network coverage on the floor is unreliable. The app must be fully usable with no
connection.

- Every write goes to the IndexedDB queue first and updates the UI immediately.
  Nothing blocks on the network. No spinners on the logging path.
- A background sync drains the queue when connectivity returns. Queue entries are
  cleared **only** on server acknowledgement.
- `POST /events` is idempotent on the client-generated UUID: re-posting an existing
  ID returns the stored event, does not error, does not duplicate. Assume responses
  get lost and clients retry.
- Reference data (items, stages, stations, routes, current drawing revisions) is
  cached locally and refreshed on each successful sync. Lookups must work offline,
  not just writes.
- Reports run on `occurred_at`. `received_at` exists to detect late syncs and wrong
  device clocks — flag events where the gap is implausible rather than trusting
  device time silently.
- The UI always shows pending-event count and last-synced time. If users cannot tell
  whether their entries landed, they stop trusting the system.

## v1 scope

Build only these:

- Login
- Item list, filterable by project and stage
- Event logging: four fields, under fifteen seconds, one-handed on a phone
- WIP by stage and station, with queue depth
- Aging report: days in current state, sorted descending

### Explicitly out of scope

Do not build these without being asked, and do not add hooks "for later":
photo uploads, push notifications, Gantt or timeline views, client portal, cost or
costing data, accounting integration, supplier portal, dashboards beyond the two
reports above, role hierarchies beyond user/admin.

## Conventions

- Migrations for every schema change; no manual DDL.
- Seed data lives in `seeds/`, is idempotent, and is the only place stage and station
  codes appear as literals outside tests.
- Server sets `received_at`; never trust a client for it.
- All timestamps stored UTC, tz-aware.
- Tests: any new report or aggregation needs a test that passes with an extra stage
  inserted at runtime. If adding a stage to the fixture breaks a test, the code has
  a hardcoded assumption — fix the code, not the test.

## Adding a new stage (the procedure this design exists to support)

1. Insert a row into `stages` with the appropriate behaviour flags.
2. Insert `stations` rows if the stage has physical instances.
3. Create a new version of any affected `route_templates`, with steps renumbered
   using the gaps.
4. Nothing else. No deploy, no migration, no code change.

If a stage cannot be added this way, that is a bug in the design and should be
fixed rather than patched around.

## Glossary

- **Item** — a batch of identical pieces tracked as one unit, with a quantity.
- **Stage** — a step type in production (paint, veneer, …).
- **Station** — a physical instance of a stage; there are two paint stations.
- **Route** — the ordered sequence of stages a product family passes through.
- **Release** — the handoff from technical office to production, at a specific
  drawing revision. Production builds only against a released revision.
- **Revision bump** — a drawing revision issued after release; flags any quantity
  currently in flight.
- **Outsourced** — work at an external supplier. Modelled as a stage with
  `requires_external_po`, where time in state is supplier lead time.
