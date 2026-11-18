# Life Style Track

Production tracking for a bespoke contract furniture factory. Engineers on the
floor log the movement of item batches through production stages from their
phones; the system answers where everything is, which stage is backed up, and
what has been sitting too long.

The design rules live in [CLAUDE.md](CLAUDE.md) and are load-bearing: an
event log as the sole source of truth, stages and stations as runtime
data rather than code, units that move freely between stages, and a fully
offline-capable PWA with an IndexedDB write queue.

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
schedule. **Office → Status** shows when the last one landed and warns when
a night has been missed. Run one on demand:

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

The suite includes extensibility tests that insert a brand-new stage at runtime
and assert both reports (and the import) pick it up, a test
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
   `requires_external_po`, `allows_partial_qty` — off means a move into the
   stage must take the whole lot it draws from), its sort order (only the
   order stages are listed in), and `max_days_in_state` — the per-stage aging
   threshold; empty means the stage is never flagged, which is what outsourced
   work wants.
2. Add stations if the stage has physical instances. Existing stations are
   renamed, reordered and retired in the same place: a rename re-labels the
   work already logged there (events point at the station id, so history is
   not rewritten), and a retired station drops off the logging picker while
   staying named in the reports. Station codes are fixed — retire a station
   and add the replacement rather than repurposing one.
3. Done. Every item, new or already on the floor, can be logged at the stage
   on the phone's next sync. Items have no stage list of their own, so there
   is nothing else to update.

`event_types` and `reason_codes` extend the same way — every
behavioural difference is a flag column, and business logic branches only on
flags, never on codes. Seeds are insert-if-missing only, so flags tuned in the
UI survive restarts.

## Importing a project

**Office → Import.** A project and all its items arrive as one spreadsheet
(.xlsx or .csv), one row per item, header row first:

| project | project name | client | item | description | qty | sample qty | revision | area | phase |
|---|---|---|---|---|---|---|---|---|---|
| HOTEL-B | Grand Hotel | Acme | BST-1 | Bedside table | 120 | 1 | A | Guest rooms | Phase 1 |
| HOTEL-B | | | BST-1 | Bedside table | 60 | 1 | A | Guest rooms | Phase 2 |

`project`, `item` and `qty` are required. `area` and `phase` are optional:
which part of the job, and which phase of it, the batch is for. The same item
code may appear on several rows as long as each has a different area or phase
— those are separate batches. A name that is not on the list yet is added to
it on import, and the preview says so first. A new project code creates the
project; an existing one is added to (never renamed by the file). There is no
`stages` column: an item is not given a route. A file written for the older
format still imports — the column is skipped, and the preview says so.

The file is previewed first — projects, items, and every problem by
spreadsheet row — and committed only if the whole file is clean.

**Pictures.** In an .xlsx, a picture placed on an item's row (Insert →
Pictures → *Place over Cells*, any column) becomes that item's icon when the
file is committed. The preview shows which items have one. A picture that
cannot be used — not JPEG/PNG/WebP/GIF/HEIC, over 5 MB, or placed *inside* a
cell — is left out with a note; it never stops the items importing. A CSV
cannot carry pictures. The file may be up to 40 MB.

**One item at a time.** **Office → Items → New item** adds a single item to an
existing project without a file: project, code, description, batch qty, sample
qty, revision. Any engineer can do it.

**Sample first.** Every item starts as its sample (`sample qty`, default 1).
Only the sample is on the floor until it is logged to Done; that entry releases
the batch (`qty`). The release is derived from the log, so voiding the
finishing entry takes the batch back off the floor.

Creating an item *is* the handoff to the floor: there is no separate release
step and nothing to set up. It can be logged at any stage straight away.

## Areas and phases

An item is **project + code + area + phase**. The king bed base for the phase 1
rooms and for the phase 2 rooms are two items with the same code, each with its
own quantity, sample and history, so production can be followed per area and
per phase.

- **Office → Areas & phases** (admin) keeps the two lists: add, rename,
  reorder, retire. They are shared by every project. Renaming one re-labels
  every item filed under it; retiring one takes it off the pickers and leaves
  its items where they are.
- An item gets its area and phase from the import file, the New item form, or
  its editor. Both are optional.
- The **Items** list filters by area, phase and progress (haven't started / in
  progress / finished); **Reports** narrow WIP and aging to an area and a
  phase. Wherever an item is shown, its area and phase are shown with its
  code.

## Logging

Units move freely. An item has no sequence: its units rest at "not started", at
any stage, or in Done, and an entry moves some of them **from one place to
another**, in any direction. On the phone: tap a batch on the item card, pick
where it went (any stage, or Done), log. The stage picked last time from that
place starts out selected, so the usual move is still two taps.

Every entry records both ends — where the units came from and where they are
now — plus quantity, station, time and who. With no sequence to consult, that
row is the record of the path a batch actually took, kept so real routes can be
read out of the log later.

There is no queued / in progress / completed within a stage. Sending work back
can be a plain move or a "Rework return" (which needs a reason and marks the
units reworked); only rework takes a finished piece back out of Done. Moving
units to the stage they are already at is only offered where the stage has
stations, as a change of booth.

Entries logged while items still had sequences name a step and no source, and
entries from before states were removed carry a state. Both still replay
exactly — see `app/ledger.py`.

## How the pieces fit

- `app/models.py` — schema. `events` rows are never edited (a mistake is
  superseded by a correction); `items` carries no derived state.
- `app/ledger.py` — the derivation core. Replays an item's events into FIFO
  lots at not started, each stage and Done, gates the batch behind its sample,
  and replays legacy state rows; produces WIP, aging, completion and every
  anomaly (competing/orphaned corrections, clock drift, legacy over-advance).
  Pure logic, no I/O.
- `app/services/` — `derivation.py` loads and replays; `reports.py` builds the
  WIP, aging and exceptions payloads; `events.py` is the idempotent write path
  and the over-advance guard; `items.py` places any starting quantities,
  archives, and records revision bumps; `imports.py` reads, checks
  and commits a project file.
- `app/api/` — FastAPI routers: auth, reference (the offline cache payload),
  items, imports, office (projects, stages, stations), images, events
  (single + batch), reports.
- `static/` — the PWA. `js/db.js` is the IndexedDB queue and cache; `js/app.js`
  renders everything from the reference payload and never names a stage or
  event type by its code. It syncs every 30 seconds but only redraws when
  the payload actually changed (ages are compared at the one decimal place the
  UI prints), and a redraw carries the user's typing, ticked boxes, open panels
  and scroll position across — a refresh must never cost someone their work.
  `sw.js` caches the shell (server-stamped version),
  drains the queue via Background Sync, and serves last-known API data offline
  with a staleness marker.
- `migrations/`, `seeds/`, `manage.py` — Alembic, insert-if-missing seed data
  (the only place codes appear as literals outside tests), and the admin CLI
  (users, backup, restore, bootstrap, demo).

### Decisions worth knowing about

- **Over-quantity moves are rejected, not stored.** An entry may move no more
  than rests at the place it takes from. The server replays the item's whole
  log in `occurred_at` order with the candidate included and rejects only if
  that adds an over-advance, so one phone's offline chain of moves validates
  whatever order it arrives in. A rejected entry is quarantined on the phone
  (tap the sync pill) with the server's reason, a Retry and a Discard; retry is
  the recovery path once the entry it depends on lands from another phone, or
  a correction frees the units.
- **`POST /api/events` is idempotent on the client UUID.** Re-posts return the
  stored row; a re-post with a *different* body (any client-controlled field,
  timestamps included) returns the stored row and marks the response
  `divergent`. Duplicate-insert races roll back via SAVEPOINT so a batch drain
  never loses its neighbours; batches apply in `occurred_at` order.
- **Two user columns.** `user_id` is who saw the work happen (client-claimed,
  because floor devices are shared); `submitted_by_user_id` is the
  authenticated session that posted the row, always server-set. The audit
  trail holds even when a colleague's phone drains your queue.
- **Aging is per item + stage**, FIFO: 40 units at paint and 50 at QC
  are two rows with independent ages, and a partial advance doesn't reset the
  clock on what stayed behind.
- **Rework is a label, not a direction.** Any move can go "backwards". A
  rework entry is a move with a reason that increments the units' rework
  count; completed-count reports exclude re-completions — no double-counting.
- **The item cards show the queue.** The phone applies its unsynced entries on
  top of the server's last answer, so a batch moved in a dead spot appears
  where it was sent and can be tapped and moved on before anything has synced.
- **Deleting is possible, archiving is the default.** Archiving an item — or a
  whole project with its items — takes it off every list and report, keeps its
  history, and can be undone from the item editor. "Delete for good" removes
  the item and its logged events; it is admin-only and written to the server
  log. Archive finished jobs: every active item is replayed by every report.
- **Phones only refetch the item list when something changed.** The reference
  payload carries a token computed from the tables (`app/services/sync.py`);
  a phone holding the same token skips the replay behind `/api/items`.
- **Done is not a stage.** Units reach it from anywhere through an event type
  flagged `is_finish`, which names no stage. The phone offers it as "Done ✓"
  alongside the stages.
- **`POST /api/items` accepts an initial per-stage distribution** for a batch
  that is already part-built when it reaches the system (with `sample_qty` 0);
  the placements are ordinary moves out of "not started".
- **Item photos** are online-only by design: the offline guarantee protects
  the logging path, and multi-megabyte blobs don't belong in its sync queue.
  Files are stored by row id, format-sniffed on upload, and served only
  through an authenticated endpoint. On a phone, adding one asks camera or
  gallery — `capture` alone replaces the picker with the camera, which is
  wrong for the shot already sitting in the camera roll. Deleting a photo is
  admin-only and lives in the item editor (Office → Items), away from the
  logging screen; it takes the file off storage with the row.
- **Sessions are revocable.** Tokens carry a digest of the password hash;
  a password reset (admin UI or `manage.py set-password`) kills every session
  issued before it. Signing out clears the device's cached identity and data
  cache. User accounts are managed in the app: Office tab → Users (admin).
- **Account status is a page, the banner is a broadcast.** Office tab → Status
  (admin) lists every account's last sign-in, last logged entry and lifetime
  entry count — how you spot a phone that stopped reaching the server, which
  the production reports cannot tell you. The same page publishes a
  green/yellow/red/neutral banner with an optional message to everyone who is
  not an admin. It travels in the reference payload, so a phone that goes into
  a dead spot keeps showing the last notice it received; neutral with no
  message shows nothing. Banners are appended, never edited — who raised the
  red and when stays in the record.
- **"Sitting too long" is per stage, not one number.** Each stage carries an
  optional `max_days_in_state`; the aging report and item cards flag against
  it, so paint runs hot at 3 days while outsourced rests for 3 weeks
  unflagged. Set it in the stage admin UI.
