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
into `stages`. Every item can be logged at it straight away, with no further setup.

Concretely, this means:

- No Python `Enum` or TypeScript union type listing stage or station names.
- No string literals of stage codes anywhere outside seed data and tests.
- No `if stage.code == "paint"` or `match stage.code` branching in business logic.
- No report, query, or UI component that hardcodes a set of columns per stage.
  Aggregations iterate over whatever is in `stages`.
- No CHECK constraints enumerating stage codes.

Anything stage-specific must be expressed as a **flag or attribute on the stage row**
(e.g. `requires_external_po`, `allows_partial_qty`, `requires_station`), never as a branch
on its name. If a new behaviour is needed, add a column to `stages`, give it a
default that preserves current behaviour, and branch on the column.

The same applies to `event_types` and `reason_codes`: table-driven, not enums.

### 3. Units move freely; the entry is the record of the path

An item has no stages of its own: no sequence, no route, no template. Its units
rest in places — "not started", any stage in the factory, or Done — and an entry
moves some of them from one place to another, in any direction. Nothing has to
be visited first and nothing has to be last.

So every entry that moves units names both ends: the stage they are at now
(`stage_id`; none on a finish, which puts them in Done) and the place they came
from (`from_stage_id`, or `from_unstarted`, or `from_done`). That is not
optional and is never inferred. With no sequence to consult, the row is the
only record of the path a batch actually took, and it is kept so the real
routes can be read back out of the log later.

Do not reintroduce an order between stages: no "next stage", no "upstream", no
picker that hides stages an item is not expected to reach, no report that
assumes a direction. `stages.sort_order` is the order stages are *listed* in,
nothing more. The phone may remember where units last went from a place and
offer that first — a default the engineer can see and change, not a rule.

`item_steps` and `events.item_step_id` are legacy. Items used to carry an
ordered sequence, and an entry named a step and no source: it took the nearest
units before that step. Those rows are in the log and replay exactly as they
did (`app/ledger.py`), and that shape is still accepted on `POST /events` so a
phone holding old queued entries can drain. Nothing creates a step any more.

### 4. Quantities live on events

An item is usually a batch (e.g. 120 identical bedside tables). Events carry a `qty`,
so a batch can partially advance. Never assume an item moves as an indivisible unit.

Quantity at a place is derived by replaying events. Moving more units than rest
at the place the entry takes them from is a **validation error** (422), never
silently stored. The check simulates the item's whole log in `occurred_at` order
with the candidate event included, so an offline chain of moves validates
against itself whatever order it arrives in. An entry that reaches the server
before the one it depends on (a move out of paint before the move into it, from
another phone) is refused and quarantined on the phone with the reason and a
Retry button, which is how it resolves once the other entry lands or a
correction frees the units. Rows that predate the guard still surface as
`over_advance` anomalies in the exceptions view.

### 5. Sample first, then the batch — derived, not stored

Every item starts as a sample (`items.sample_qty`, default 1). Until the sample has
been logged Done — by whatever path it takes — only the sample exists on the
floor. The moment it reaches Done, the production batch (`total_qty`, on top of
the sample) is released into "not started".

The release is computed by the ledger on every replay, timestamped by the event
that finished the sample. It is never a column, never a stored flag, never an
event the server writes: void the finishing entry and the batch goes back to not
existing. `sample_qty` 0 means no sample (items that predate the flow).

### 6. A unit is at a stage or it is not

There are no per-stage states (no queued / in progress / completed). An event
says "these units are now at this stage"; an event of a type flagged `is_finish`
moves them into Done, from wherever they are. A move within one stage only
means something as a change of station, and does not restart the units' age.
`event_states` and `events.state_id` survive only because the log already holds
rows that reference them: the ledger replays those in per-state slots inside
their step, and reads every slot of a stage as "at that stage". Never write a
new state; never add one.

### 7. Rework is a labelled move, not a reversal

Sending work back is an entry like any other — it names where the units came
from and where they went. What makes it rework is its type: an event type
flagged `is_rework` requires a `reason_code` and marks the units as reworked.
It is never a deleted event or an edit to the entry being "undone".

Units can go back to an earlier stage as a plain move too; that is allowed and
simply is not counted as rework. The one thing only rework can do is take units
back out of Done, because a finished piece coming back always has a reason.

Throughput and cycle-time reports must exclude rework quantities from completed
counts, or they will double-count.

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
  the sample started counting. Items arrive through the project import file,
  or one at a time from Office → Items. An item has no stages of its own
- `item_steps` — legacy only (see rule 3): the ordered sequence items used to
  carry. Never written; read to replay entries that point at a step
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
| `stage_id` | where the units are now. NULL on a finish entry (Done) and on item-level events |
| `station_id` | nullable; set where the stage has stations |
| `event_type_id` | |
| `qty` | |
| `from_stage_id` | where the units came from, if that was a stage |
| `from_unstarted` | they came from "not started" |
| `from_done` | they came back out of Done; rework only |
| `item_step_id`, `from_item_step_id` | legacy only (rule 3): sequence-era rows name steps |
| `state_id` | legacy only, NULL on every new row (see rule 6) |
| `reason_code_id` | nullable |
| `occurred_at` | device time, when it happened on the floor |
| `received_at` | server time, set on insert |
| `user_id` | who saw the work happen (client-claimed, for shared floor phones) |
| `submitted_by_user_id` | the authenticated session that posted the row; server-set |
| `note` | nullable, free text, never parsed |
| `supersedes_event_id` | nullable, for corrections |

Exactly one of `from_stage_id` / `from_unstarted` / `from_done` is set on every
new entry that moves units.

Index for the queries that matter: `(item_id, occurred_at)`,
`(station_id, occurred_at)`, `(received_at)`, `(stage_id)`.

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
  Tap a batch on the item card (that is "from"), pick where it went — any
  stage, or Done — and log. The stage picked last time from that place starts
  out selected. No state picker
- The item cards show queued entries at once: `localPlaces()` in
  `static/js/app.js` applies the phone's unsynced queue on top of the server's
  last answer, so a batch moved in a dead spot can be tapped and moved on
- WIP by stage and station (depth = units at the stage), with sample units shown
- Aging report: days at current stage, sorted descending
- Exceptions view: clock drift, late syncs, competing/orphaned corrections and
  legacy over-advance rows — flags derived from the log, not a third dashboard.
  Clock and late-sync flags drop off `LSF_CLOCK_FLAG_DAYS` (30) after the entry
  reached the server; flags about something still wrong never do
- Office → Import: projects and their items arrive as one spreadsheet (.xlsx or
  .csv, one row per item: project, item, qty, sample qty, revision).
  Previewed, then committed all-or-nothing. Format lives in
  `app/services/imports.py`; `GET /api/import/template` returns a template.
  A `stages` column from an older file is read past, and the preview says so.
  A picture placed over the cells of an item's row in an .xlsx becomes that
  item's icon on commit; one that cannot be used (wrong format, over 5 MB,
  placed *inside* a cell) is left out with a note and never blocks the import.
  Online-only like every other photo: the bytes never enter the sync queue
- Office → Items → New item: one item added to a project by hand, without an
  import, by any engineer. It posts `POST /api/items`, which also takes an
  optional mid-production distribution by stage (with `sample_qty` 0)
- Office tab: projects, item editing, and admin management of stages/stations
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
3. Nothing else. No deploy, no migration, no code change. Every item — new or
   already on the floor — can be logged at the stage on the phone's next sync.

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
- `stages.is_terminal` is legacy: it marked the stage that closed a sequence.
  It is off the admin screen and read only when replaying rows that carry a
  legacy state. `allows_partial_qty` false now means a move into the stage must
  take everything resting at the place it comes from.
- An entry naming a source is refused if it arrives before the entry that put
  the units there. One phone's own chain always arrives together and is ordered
  by the server; across phones the refused entry waits under the sync pill with
  Retry. This replaced the old "skipped stages resolve themselves" behaviour,
  which depended on a sequence to walk back along.
- After a deploy the service worker swaps in the new shell, and the page
  reloads onto it (never while a field has focus). A phone still running the
  script from before sequences were removed needs the app reopened once.

## Glossary

- **Item** — a batch of identical pieces tracked as one unit, with a quantity.
- **Sample** — the first unit(s) of an item, built before the batch exists.
  Logging it Done releases the batch.
- **Place** — where units can rest: not started, a stage, or Done.
- **Done** — where finished units rest. Not a stage; reached by a finish entry.
- **Stage** — a step type in production (paint, veneer, …).
- **Station** — a physical instance of a stage; there are two paint stations.
- **Move** — an entry: some units, from one place to another. Any direction.
- **Revision bump** — a drawing revision issued after the item is in production;
  flags any quantity currently in flight.
- **Outsourced** — work at an external supplier. Modelled as a stage with
  `requires_external_po`, where time in state is supplier lead time.
