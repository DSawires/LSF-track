#!/bin/sh
# Container start: wait for the database, migrate, seed, ensure an admin exists,
# then serve. Everything before uvicorn is idempotent, so restarts are safe.
set -e

# Sessions must survive restarts (a signed-out phone cannot drain its offline
# queue), so the app refuses to boot without a secret key. If the operator did
# not provide one, generate it once and persist it on the data volume — the
# same key comes back on every restart and redeploy.
if [ -z "$LSF_SECRET_KEY" ]; then
  key_file="${LSF_UPLOAD_DIR:-data/uploads}/../secret_key"
  if [ ! -s "$key_file" ]; then
    mkdir -p "$(dirname "$key_file")"
    python -c "import secrets; print(secrets.token_urlsafe(48))" > "$key_file"
    chmod 600 "$key_file"
    echo "generated a persistent LSF_SECRET_KEY at $key_file (set one in .env to manage it yourself)"
  fi
  LSF_SECRET_KEY="$(cat "$key_file")"
  export LSF_SECRET_KEY
fi

echo "waiting for database..."
tries=0
until python -c "from app.db import get_engine; get_engine().connect().close()" 2>/dev/null; do
  tries=$((tries + 1))
  if [ "$tries" -ge 60 ]; then
    echo "database did not come up after 120s" >&2
    exit 1
  fi
  sleep 2
done

python manage.py migrate
python manage.py seed
python manage.py bootstrap

if [ "$LSF_DEMO" = "true" ]; then
  python manage.py demo
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 "$@"
