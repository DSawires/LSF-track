FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/lsf

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

RUN useradd --create-home lsf
USER lsf

EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
