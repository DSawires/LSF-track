FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/lsf

# pg_dump matching the postgres:16 service — bookworm's own client is v15,
# which refuses to dump a newer server, so it comes from the pgdg repo.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-common ca-certificates curl gnupg \
    && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, so code edits don't bust this layer. The install reads the
# dependency list straight from pyproject.toml — one source of truth.
COPY pyproject.toml ./
RUN pip install .

COPY alembic.ini manage.py ./
COPY --chmod=755 docker-entrypoint.sh ./
COPY app app
COPY migrations migrations
COPY seeds seeds
COPY static static

RUN useradd --create-home lsf \
    # Present in the image so the named volume inherits this ownership on first
    # mount; without it the volume comes up root-owned and uploads fail.
    && mkdir -p /srv/lsf/data/uploads \
    && chown -R lsf /srv/lsf/data
USER lsf

EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
