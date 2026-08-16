# LSF Track

Production tracking for a bespoke contract furniture factory. Engineers on the
floor log the movement of item batches through production stages from their
phones; the system answers where everything is, which stage is backed up, and
what has been sitting too long.

The design rules live in [CLAUDE.md](CLAUDE.md) and are load-bearing: an
append-only event log as the sole source of truth, stages/stations/routes as
runtime data rather than code, routes snapshotted onto items at release, and a
fully offline-capable PWA with an IndexedDB write queue.

## Quickstart (Docker — the intended path)

```sh
git clone <repo> && cd LSF-track
docker compose up
```

That's the whole deployment: PostgreSQL plus the app, migrated, seeded, and
with an admin account ready. Open `http://localhost:8000` and sign in as
`admin` / `admin`.

Configuration goes in a `.env` file next to `docker-compose.yml` (all
optional):

```sh
LSF_SECRET_KEY=<real random key>      # do set this for anything non-throwaway
LSF_ADMIN_USERNAME=dave               # first admin, created on first boot only
LSF_ADMIN_PASSWORD=...
POSTGRES_PASSWORD=...
LSF_PORT=8000                         # host port
LSF_DEMO=true                         # pre-load sample factory data
```

The compose stack serves plain HTTP with `LSF_SECURE_COOKIES=false`; for the
factory, put TLS termination (Caddy, nginx, Tailscale) in front and set it
back to true. Data lives in the `pgdata` volume; `docker compose down -v`
erases it.

## Quickstart (bare, without Docker)

```sh
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"

cp .env.example .env            # then edit; see below
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed              # idempotent reference data
.venv/bin/python manage.py create-user dave "Dave S" --admin

.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://<host>:8000/` on a phone and add it to the home screen. For a
throwaway environment with realistic in-flight data:

```sh
.venv/bin/python manage.py demo    # migrate + seed + sample projects/items/events
                                   # then sign in as demo / demo
```

Production runs on PostgreSQL (`LSF_DATABASE_URL=postgresql+psycopg://…`).
SQLite works and is what the test suite and demo use, but don't run the factory
on it.

### Environment

| Variable | Meaning |
|---|---|
| `LSF_DATABASE_URL` | SQLAlchemy URL; PostgreSQL in production |
| `LSF_SECRET_KEY` | Session cookie signing key — set a real one |
| `LSF_SESSION_MAX_AGE_DAYS` | Sliding session lifetime (default 30). Long on purpose: an engineer must never be logged out mid-shift while offline, or their queue can't drain |
| `LSF_SECURE_COOKIES` | `false` only for local HTTP development |
| `LSF_MAX_DEVICE_AHEAD_SECONDS` | `occurred_at` this far ahead of server receipt flags a wrong device clock |
| `LSF_MAX_SYNC_LAG_DAYS` | events syncing later than this are flagged as late |

## Tests

```sh
.venv/bin/python -m pytest
```

The suite includes extensibility tests that insert a brand-new stage *and* a
brand-new event state at runtime and assert both reports pick them up, plus a
grep test that fails the build if any seeded stage code appears as a string
literal inside `app/`. If adding a stage to a fixture breaks a test, fix the
code, not the test.

## Adding a stage (no deploy, no migration, no code)

1. Insert a row into `stages` with the behaviour flags
   (`requires_station`, `requires_external_po`, `allows_partial_qty`,
   `is_terminal`, `sort_order`).
2. Insert `stations` rows if the stage has physical instances.
3. Create a new `route_templates` version with the step slotted in
   (sequence numbers leave gaps of 10 for exactly this).
4. Done. Items already in production keep the route they were released
   against; newly released items pick up the new version.

`event_types`, `event_states` and `reason_codes` extend the same way — every
behavioural difference is a flag column, and business logic branches only on
flags, never on codes.

**Projects and route templates** are created in the app: Office tab → "New
project" / "New route". Posting a route under an existing code creates the next
version; items already released keep the version they left against.

## How the pieces fit

- `app/models.py` — schema. `events` is insert-only; `items` carries no derived
  state.
- `app/ledger.py` — the derivation core. Replays an item's events into FIFO
  lots at (step, state) positions; produces WIP, aging, completion and every
  anomaly (over-advance, competing corrections, clock drift). Pure logic, no
  I/O.
- `app/services/` — `derivation.py` loads and replays; `reports.py` builds the
  WIP, aging and exceptions payloads; `events.py` is the idempotent write path;
  `release.py` snapshots a route template onto an item.
- `app/api/` — FastAPI routers: auth, reference (the offline cache payload),
  items + release, events (single + batch), reports.
- `static/` — the PWA. `js/db.js` is the IndexedDB queue and cache; `js/app.js`
  renders everything from the reference payload and never names a stage, state
  or event type by its code.
- `migrations/`, `seeds/`, `manage.py` — Alembic, idempotent seed data (the
  only place codes appear as literals outside tests), and the admin CLI.

### Decisions worth knowing about

- **Writes are accepted, not judged.** An event that reaches the server is
  stored unless it is structurally unprocessable (unknown ids, wrong item).
  A batch advancing more units than exist upstream is stored and surfaced in
  the exceptions report — the entry was made hours ago on a phone with no
  signal, and silently dropping it at sync time is the one failure the system
  can't afford. Corrections (`supersedes_event_id`) are how mistakes die.
- **`POST /api/events` is idempotent on the client UUID.** Re-posts return the
  stored row; a re-post with a *different* body still returns the stored row
  and marks the response `divergent`. Duplicate-insert races roll back via
  SAVEPOINT so a batch drain never loses its neighbours.
- **Aging is per item + step + state**, FIFO: 40 units at paint and 50 at QC
  are two rows with independent ages, and a partial advance doesn't reset the
  clock on what stayed behind.
- **Rework is forward motion.** A rework event pulls units back from
  downstream positions, increments their rework count, and completed-count
  reports exclude re-completions — no double-counting.
- **`state` is `state_id`**, a foreign key to `event_states` — the spec's
  lookup-table intent, applied literally so states are as extensible as stages.
