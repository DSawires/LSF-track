#!/bin/sh
# Container start: wait for the database, migrate, seed, ensure an admin exists,
# then serve. Everything before uvicorn is idempotent, so restarts are safe.
set -e

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
