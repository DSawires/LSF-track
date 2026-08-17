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

That's the whole deployment: PostgreSQL, the app (migrated and seeded), a
daily backup job, and a Caddy proxy. Open `http://localhost/` (the proxy
serves port 80; change it with `LSF_HTTP_PORT`).

**First sign-in:** an admin account is created on first boot. If you didn't
set `LSF_ADMIN_PASSWORD`, a password was generated and printed **once** in the
app container's logs — find it with:

```sh
docker compose logs app | grep -A3 "GENERATED password"
```

A session-signing key is likewise generated on first boot and persisted on the
`uploads` volume, so sessions survive restarts out of the box. Set
`LSF_SECRET_KEY` in `.env` to manage it yourself.

Configuration goes in a `.env` file next to `docker-compose.yml`. Copy
[.env.example](.env.example) — it documents every variable with its default.
The short version for a real deployment:

```sh
LSF_SECRET_KEY=<python -c "import secrets; print(secrets.token_urlsafe(48))">
LSF_ADMIN_PASSWORD=<a real one>
POSTGRES_PASSWORD=<a real one>
LSF_DOMAIN=track.example.com     # Caddy gets TLS from Let's Encrypt
LSF_SECURE_COOKIES=true          # mandatory once LSF_DOMAIN is set (enforced at boot)
```

Without `LSF_DOMAIN` the proxy serves plain HTTP — fine for a LAN or a first
look, not for the public internet. The app **refuses to boot** with
`LSF_DOMAIN` set while `LSF_SECURE_COOKIES` is false, and with a missing,
short, or placeholder secret key.

Set `LSF_DEMO=true` for a throwaway instance pre-loaded with sample data
(sign in as `demo` / `demo` — a non-admin account; the demo loader refuses
to run against a database that already holds real projects).

Data lives in the `pgdata` volume; `docker compose down -v` erases it.

### Photos and backups: S3

Set `LSF_S3_BUCKET` and one private bucket holds both item photos (`images/`)
and nightly `pg_dump` archives (`backups/`, newest `LSF_BACKUP_KEEP` kept).
The `backup` service dumps shortly after boot and then daily at 02:00 UTC —
anchored to the clock, not container start, so redeploys can't starve the
schedule. Run one on demand:

```sh
docker compose exec backup python manage.py backup
```

Without a bucket, dumps land on the local `uploads` volume instead — fine for
a laptop, not durable for production.

On EC2, skip AWS keys entirely: attach an instance role with

```json
{"Effect": "Allow",
 "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:ListBucket"],
 "Resource": ["arn:aws:s3:::YOUR-BUCKET", "arn:aws:s3:::YOUR-BUCKET/*"]}
```

and raise the metadata hop limit so containers can reach role credentials
(the default of 1 silently blocks them):

```sh
aws ec2 modify-instance-metadata-options --instance-id i-… \
  --http-put-response-hop-limit 2 --http-tokens required
```

### Restore (rehearse this before you need it)

```sh
docker compose stop app                                   # events written during a
docker compose exec backup python manage.py restore --list    # restore are lost
docker compose exec backup python manage.py restore \
    backups/lsf-20260816-020000Z.sql.gz --reset --yes
docker compose start app
```

`--reset` drops and recreates the `public` schema first, which a dump made by
`manage.py backup` needs when restoring onto a non-empty database. A local
file path works in place of a storage key.

### Upgrading

```sh
git pull
docker compose up -d --build
```

The entrypoint migrates on boot. Sessions survive (the key is persisted), and
every phone's service worker picks up the new frontend automatically — the
shell cache is stamped with a hash of `static/`, so there is no version
constant to remember to bump. Take a backup before upgrading:
`docker compose exec backup python manage.py backup`.

## Quickstart (bare, without Docker)

```sh
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"

cp .env.example .env            # then edit; LSF_SECRET_KEY is mandatory
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
SQLite works and is what the local test suite and demo use, but don't run the
factory on it.

Note the app expects to run as **one** uvicorn worker (the shipped
configuration): the login throttle is in-memory and per-process. At this
factory's size one worker is plenty.

## Tests and CI

```sh
.venv/bin/python -m pytest
```

The suite includes extensibility tests that insert a brand-new stage *and* a
brand-new event state at runtime and assert both reports pick them up, a test
that performs the whole add-a-stage procedure through the admin HTTP API, plus
a grep test that fails the build if any seeded stage code appears as a string
literal inside `app/`. If adding a stage to a fixture breaks a test, fix the
code, not the test.

CI (`.github/workflows/ci.yml`) runs the suite twice — on SQLite and against
a real PostgreSQL 16 (`LSF_TEST_DATABASE_URL`), where SAVEPOINT semantics,
varchar limits and native UUIDs actually differ — and additionally runs the
Alembic migrations from an empty database, `alembic check` against the models,
and the seed twice for idempotency.

Runtime dependencies are pinned in `requirements.lock` (consumed by the
Dockerfile). To bump: create a fresh venv, `pip install .`, then
`pip freeze --exclude lsf-track > requirements.lock`.

## Adding a stage (no deploy, no migration, no code)

Done in the app by an admin: **Office tab → Stages & stations**.

1. Add the stage with its behaviour flags (`requires_station`,
   `requires_external_po`, `allows_partial_qty`, `is_terminal`, sort order,
   and `max_days_in_state` — the per-stage aging threshold; empty means the
   stage is never flagged, which is what outsourced work wants).
2. Add stations if the stage has physical instances. Existing stations are
   renamed, reordered and retired in the same place: a rename re-labels the
   work already logged there (events point at the station id, so history is
   not rewritten), and a retired station drops off the logging picker while
   staying named in the reports. Station codes are fixed — retire a station
   and add the replacement rather than repurposing one.
3. In "New route", create the next version of any affected route — the
   builder has ＋ insertion points to slot the stage between existing steps.
4. Done. Items already in production keep the route they were released
   against; newly released items pick up the new version.

`event_types`, `event_states` and `reason_codes` extend the same way — every
behavioural difference is a flag column, and business logic branches only on
flags, never on codes. Seeds are insert-if-missing only, so flags tuned in the
UI survive restarts.

**Projects and route templates** are created in the app: Office tab → "New
project" / "New route". Posting a route under an existing code creates the next
version; items already released keep the version they left against.

## How the pieces fit

- `app/models.py` — schema. `events` is insert-only; `items` carries no derived
  state.
- `app/ledger.py` — the derivation core. Replays an item's events into FIFO
  lots at (step, state) positions; produces WIP, aging, completion and every
  anomaly (competing/orphaned corrections, clock drift, legacy over-advance).
  Pure logic, no I/O.
- `app/services/` — `derivation.py` loads and replays; `reports.py` builds the
  WIP, aging and exceptions payloads; `events.py` is the idempotent write path
  and the over-advance guard; `release.py` snapshots a route template onto an
  item.
- `app/api/` — FastAPI routers: auth, reference (the offline cache payload),
  items + release, office (projects, routes, stages, stations), images,
  events (single + batch), reports.
- `static/` — the PWA. `js/db.js` is the IndexedDB queue and cache; `js/app.js`
  renders everything from the reference payload and never names a stage, state
  or event type by its code; `sw.js` caches the shell (server-stamped version),
  drains the queue via Background Sync, and serves last-known API data offline
  with a staleness marker.
- `migrations/`, `seeds/`, `manage.py` — Alembic, insert-if-missing seed data
  (the only place codes appear as literals outside tests), and the admin CLI
  (users, backup, restore, bootstrap, demo).

### Decisions worth knowing about

- **Over-quantity moves are rejected, not stored.** The server replays the
  item's whole log in `occurred_at` order with the candidate included and
  rejects only if that adds an over-advance — so out-of-order sync is never
  falsely refused. A rejected entry is quarantined on the phone (tap the sync
  pill) with the server's reason, a Retry and a Discard; retry is the recovery
  path once the missing upstream entry lands or a correction frees the units.
- **`POST /api/events` is idempotent on the client UUID.** Re-posts return the
  stored row; a re-post with a *different* body (any client-controlled field,
  timestamps included) returns the stored row and marks the response
  `divergent`. Duplicate-insert races roll back via SAVEPOINT so a batch drain
  never loses its neighbours; batches apply in `occurred_at` order.
- **Two user columns.** `user_id` is who saw the work happen (client-claimed,
  because floor devices are shared); `submitted_by_user_id` is the
  authenticated session that posted the row, always server-set. The audit
  trail holds even when a colleague's phone drains your queue.
- **Aging is per item + step + state**, FIFO: 40 units at paint and 50 at QC
  are two rows with independent ages, and a partial advance doesn't reset the
  clock on what stayed behind.
- **Rework is forward motion.** A rework event pulls units back from
  downstream positions, increments their rework count, and completed-count
  reports exclude re-completions — no double-counting.
- **States are as extensible as stages.** `state` is a foreign key to
  `event_states`; the queue state is flagged `is_initial` rather than assumed
  from sort order.
- **Completing a step offers a one-tap "also queue at next stage"** (default
  on). It writes two ordinary events a millisecond apart — done here, queued
  there — so the log stays factual and the derivation needs no special case.
- **Releasing accepts an initial per-stage distribution** for items entering
  the system mid-production; the placements are ordinary queued events written
  deepest-step-first at release time.
- **Item photos** are online-only by design: the offline guarantee protects
  the logging path, and multi-megabyte blobs don't belong in its sync queue.
  Files are stored by row id, format-sniffed on upload, and served only
  through an authenticated endpoint.
- **Sessions are revocable.** Tokens carry a digest of the password hash;
  a password reset (admin UI or `manage.py set-password`) kills every session
  issued before it. Signing out clears the device's cached identity and data
  cache. User accounts are managed in the app: Office tab → Users (admin).
- **"Sitting too long" is per stage, not one number.** Each stage carries an
  optional `max_days_in_state`; the aging report and item cards flag against
  it, so paint runs hot at 3 days while outsourced rests for 3 weeks
  unflagged. Set it in the stage admin UI.
