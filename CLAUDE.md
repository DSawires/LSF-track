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

### 1. The event log is the source of truth

`events` is the source of truth. Current state is **derived** from the log by
query.

Do not add a `current_status`, `current_stage`, or `is_complete` column to `items`.
A materialized view or cached projection is acceptable **only** if it is fully
rebuildable from `events` and is rebuilt by a documented command. If a projection
and the log disagree, the log wins.

Entries are never edited in place. A wrong entry is corrected by a new event
(`event_type = 'correction'`) referencing the one it supersedes via
`supersedes_event_id`, so the log still shows what was believed and when.

Deletion is allowed. An admin can delete an item for good
(`DELETE /api/items/{id}?purge=true`), which removes its events, stages and
photos with it. It cannot be undone, so it is admin-only, confirmed in the UI by
typing the item code, and written to the server log (who, which item, how many
events) — the events table cannot record its own deletion. Archiving is still
the default way to take something off the floor: it keeps the history and can
be reversed.

### 2. Stages and stations are data, not code

The factory will add stages. Assume this will happen repeatedly and that the person
adding one will not be a developer.

Adding a stage must require **zero code changes and zero deploys**. It is an insert
into `stages`. It is pickable on the next item created, with no further setup.

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

### 3. Each item owns its stage sequence

There are no shared route templates. An item's stages are chosen on the item, when
it is created, and written to `item_steps` — in practice from the `stages` cell of
its row in the project import file. Two items only share a sequence because the
file says so twice: nothing links them afterwards, and editing one item's stages
can never reach another's.

This is what makes adding a stage safe. New items pick it up; work already on the
floor keeps the sequence its events were logged against, because there is no
template that could reach in and re-version it.

A sequence is rewritable until the floor logs against the item, and frozen from
then on — once an event points at a step, that step is what the event *means*, and
rewriting it would quietly re-label work already done.

Use sequence numbers in increments of 10 so a stage can be inserted between two
existing ones without renumbering.

### 4. Quantities live on events

An item is usually a batch (e.g. 120 identical bedside tables). Events carry a `qty`,
so a batch can partially advance. Never assume an item moves as an indivisible unit.

Quantity in a given state is derived by summing events. A batch advancing more
units than exist upstream is a **validation error** (422), never silently stored.
The check simulates the item's whole log in `occurred_at` order with the candidate
event included, so out-of-order sync is never falsely rejected; a genuine rejection
is quarantined on the phone with the reason and a Retry button, which is how it
resolves once a correction voids the conflicting entry or the missing upstream
event syncs in from another device. Rows that predate the guard still surface as
`over_advance` anomalies in the exceptions view.

### 5. Sample first, then the batch — derived, not stored

Every item starts as a sample (`items.sample_qty`, default 1). Until the sample has
walked the item's whole sequence into Done, only the sample exists on the floor.
The moment it reaches Done, the production batch (`total_qty`, on top of the
sample) is released into "not started" and follows the same stages.

The release is computed by the ledger on every replay, timestamped by the event
that finished the sample. It is never a column, never a stored flag, never an
event the server writes: void the finishing entry and the batch goes back to not
existing. `sample_qty` 0 means no sample (items that predate the flow).

### 6. A unit is at a stage or it is not

There are no per-stage states (no queued / in progress / completed). An event
says "these units are now at this stage"; an event of a type flagged `is_finish`
moves them off the end of the sequence into Done. `event_states` and
`events.state_id` survive only because the log already holds rows that reference
them: the ledger replays those in per-state slots inside their step, and reads
every slot of a step as "at that stage". Never write a new state; never add one.

### 7. Rework is forward motion, not a reversal

A piece returning from QC to an earlier stage is an event of type `rework_return`
with a `reason_code` and a target step, not a backwards transition or a deleted
event. Throughput and cycle-time reports must exclude rework quantities from
completed counts, or they will double-count.

A rework entry names where the units came back from (`from_item_step_id`, or
`from_done`), and only that place is drawn from. Without it the ledger takes
the nearest units downstream of the target, which is wrong whenever another lot
rests in between: QC returning 5 to carpentry must not take them from paint.
Rows that carry no source — everything logged before the column — still replay
nearest-first.

### 8. Facts, not judgments

No `percent_complete` field. No `status` free-text. No "on track / at risk" flag.
Users record what happened: item, stage, station, state, quantity, time. Any
health assessment is computed from the log, never typed in.

The admin floor banner is not a hole in this. It is one person's announcement to
the floor — attached to no item, read by no report, feeding no aggregate. The
moment anything starts deriving from its colour, that is the rule being broken.

## Data model

Names are indicative; Alembic migrations are authoritative.

- `projects` — client jobs
- `stages` — a step type (carpentry, veneer, paint, upholstery, outsourced, QC,
  packing, …). Carries behaviour flags. **Extensible at runtime.**
- `stations` — physical instances of a stage (paint_1, paint_2, carpentry_1, …).
  Many stations per stage. Recorded on events for load balancing.
- `items` — a batch: code, project, description, total qty (the production
  batch), sample qty, current drawing revision. Creating one **is** the handoff
  to the floor: there is no released/unreleased state, and `created_at` is when
  the sample started counting. Items arrive through the project import file
- `item_steps` — this item's own ordered stages, chosen at creation
- `events` — the log. See below.
- `event_types`, `reason_codes` — table-driven vocabularies
- `event_states` — legacy only (see rule 6); no longer seeded, never written
- `users` — carries `last_login_at`, stamped at sign-in. The only piece of
  account status not derived from the log, because signing in is not an event
  about an item.
- `status_banners` — the notice an admin puts on the floor's screens. Insert
  only, latest row wins, cleared by appending a neutral one. `color` is a fixed
  UI palette (green/yellow/red/neutral), deliberately not a lookup table: it is
  what the stylesheet can paint, not vocabulary the factory owns, and nothing
  branches on it server-side.

### `events` columns

| Column | Notes |
|---|---|
| `id` | UUID, **generated by the client** |
| `item_id` | |
| `item_step_id` | nullable for item-level events (revision bumps) and finish entries |
| `station_id` | nullable; required where the stage has stations |
| `event_type_id` | |
| `state_id` | legacy only, NULL on every new row (see rule 6) |
| `qty` | |
| `from_item_step_id` | nullable; where the units came from. The phone sets it on rework (rule 7) |
| `from_done` | the units came back out of Done; rework only |
| `reason_code_id` | nullable |
| `occurred_at` | device time, when it happened on the floor |
| `received_at` | server time, set on insert |
| `user_id` | who saw the work happen (client-claimed, for shared floor phones) |
| `submitted_by_user_id` | the authenticated session that posted the row; server-set |
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
- Reference data (items, stages, stations, current drawing revisions) is
  cached locally and refreshed on each successful sync. Lookups must work offline,
  not just writes.
- The item list is a replay of the whole log, so a phone refetches it only when
  `items_token` in the reference payload has changed (`app/services/sync.py`),
  or its copy is ten minutes old. The token is recomputed from the tables on
  every call and stored nowhere, so it is not a projection and cannot drift.
- A response the service worker served from its cache is not a sync. `sync()`
  only counts answers that came from the server, or "last synced" would read
  "just now" in a dead spot.
- Reports run on `occurred_at`. `received_at` exists to detect late syncs and wrong
  device clocks — flag events where the gap is implausible rather than trusting
  device time silently.
- The UI always shows pending-event count and last-synced time. If users cannot tell
  whether their entries landed, they stop trusting the system.

## Scope

The shipped product (v1 scope plus additions blessed 2026-08-16):

- Login, logout, session handling that survives offline shifts
- Item list, filterable by project and stage, plus free-text search
- Event logging: pre-selected fields, under fifteen seconds, one-handed on a phone.
  The picker is the item's stages plus Done; no state picker
- WIP by stage and station (depth = units at the stage), with sample units shown
- Aging report: days at current stage, sorted descending
- Exceptions view: clock drift, late syncs, competing/orphaned corrections and
  legacy over-advance rows — flags derived from the log, not a third dashboard.
  Clock and late-sync flags drop off `LSF_CLOCK_FLAG_DAYS` (30) after the entry
  reached the server; flags about something still wrong never do
- Office → Import: projects and their items arrive as one spreadsheet (.xlsx or
  .csv, one row per item: project, item, qty, sample qty, revision, stages).
  Previewed, then committed all-or-nothing. Format lives in
  `app/services/imports.py`; `GET /api/import/template` builds a template from
  the stages that exist now
- Office tab: projects, item editing, and admin management of stages/stations.
  `POST /api/items` (one item, optional mid-production distribution with
  `sample_qty` 0) remains as the API underneath the import
- Taking work off the floor (admin): archive an item, or a project together
  with its items — each archive is an event, and both can be reactivated.
  Finished jobs should be archived: every active item is replayed by every
  report. Deleting an item for good is rule 1's purge
- Office → Status (admin only): per-account last sign-in, last logged entry and
  lifetime entry count, plus the green/yellow/red/neutral banner an admin
  publishes to every non-admin. The banner ships in the reference payload, so
  it survives the phone losing signal. Also when the last database backup
  landed, read off the storage listing
- Item photos (snag photos + item icons). Online-only by design: the offline
  guarantee protects the logging path, and multi-megabyte blobs do not belong
  in its sync queue. Adding one offers camera or gallery on a phone; deleting
  one is admin-only and lives in the item editor, not on the logging screen

### Explicitly out of scope

Do not build these without being asked, and do not add hooks "for later":
push notifications, Gantt or timeline views, client portal, cost or costing
data, accounting integration, supplier portal, dashboards beyond the reports
above, role hierarchies beyond user/admin.

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

Performed by an admin in the app: Office tab → "Stages & stations".

1. Add the stage with the appropriate behaviour flags.
2. Add stations if the stage has physical instances.
3. Nothing else. No deploy, no migration, no code change. The stage can be named
   in the next import file (and appears in the downloadable template), and items
   already on the floor keep the sequence their entries were logged against.

If a stage cannot be added this way, that is a bug in the design and should be
fixed rather than patched around.

## Operational constraints worth knowing

- `LSF_SECRET_KEY` is mandatory (no ephemeral fallback): sessions must survive
  restarts or offline phones cannot drain their queues after a redeploy. The
  Docker entrypoint generates and persists one if unset. It is required by the
  **web app only**, and validated at the point of use (`require_session_key`),
  not when settings load — the backup container runs its own entrypoint, never
  serves a request, and must not lose a night's dump over a key it never signs
  anything with.
- The login throttle is in-memory and per-process: correct at one uvicorn
  worker (the shipped configuration). Adding `--workers N` needs a shared
  store for it first.
- Seeds are insert-if-missing only. They guarantee the vocabulary exists;
  they never overwrite rows, because stages and flags are runtime data owned
  by the factory.
- `POST /events` locks the item row for the length of the request, so two
  phones cannot both pass the availability check for the same item. A batch
  locks every item it names up front, in id order, so two drains cannot
  deadlock.
- `/api/docs` is off unless `LSF_API_DOCS=true`: it is served without a
  session, and the production host is on the public internet.
- `stages.is_terminal` only means "must be last in a sequence". Units finish
  through a finish entry; the flag completes units only on legacy state rows.

## Glossary

- **Item** — a batch of identical pieces tracked as one unit, with a quantity.
- **Sample** — the first unit(s) of an item, built through every stage before
  the batch exists. Its completion releases the batch.
- **Done** — the end of every sequence. Not a stage; reached by a finish entry.
- **Stage** — a step type in production (paint, veneer, …).
- **Station** — a physical instance of a stage; there are two paint stations.
- **Sequence** — the ordered stages one item passes through. Per item, not shared.
- **Revision bump** — a drawing revision issued after the item is in production;
  flags any quantity currently in flight.
- **Outsourced** — work at an external supplier. Modelled as a stage with
  `requires_external_po`, where time in state is supplier lead time.
