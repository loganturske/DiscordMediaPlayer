# syntax=docker/dockerfile:1
# Matching Debian releases keep the Node binary compatible with the Python image.
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    YOUTUBE_COOKIE_FILE=/app/data/youtube-cookies.txt
WORKDIR /app
COPY --from=node /usr/local/bin/node /usr/local/bin/node

# Optional BuildKit secret supports managed corporate/cloud HTTPS proxies without
# baking their trust bundle into the image. Ordinary local builds need no secret.
RUN --mount=type=secret,id=proxy_ca \
    build_ca=/etc/ssl/certs/ca-certificates.crt; \
    if [ -f /run/secrets/proxy_ca ]; then build_ca=/run/secrets/proxy_ca; fi; \
    apt-get -o Acquire::https::CaInfo="$build_ca" update && \
    apt-get -o Acquire::https::CaInfo="$build_ca" install -y --no-install-recommends \
        ca-certificates ffmpeg libopus0 && \
    rm -rf /var/lib/apt/lists/*

# Source edits reuse the dependency layer; runtime images contain no dev tools.
COPY --chmod=0644 requirements.txt ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 bot && \
    useradd --uid 10001 --gid bot --no-create-home --home-dir /app/data \
        --shell /usr/sbin/nologin bot && \
    mkdir -p /app/data && chown bot:bot /app/data && chmod 0700 /app/data
COPY musicbot/ ./musicbot/
# Normalize restrictive host checkout permissions while keeping source root-owned.
COPY scripts/ ./scripts/
RUN chmod -R a+rX /app/musicbot /app/scripts
USER bot:bot
STOPSIGNAL SIGTERM
CMD ["python", "-m", "musicbot"]

# Explicit validation target: docker build --target test .
FROM app AS test
USER root
COPY --chmod=0644 requirements-dev.txt pyproject.toml ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install --no-cache-dir -r requirements-dev.txt
COPY tests/ ./tests/
RUN chmod -R a+rX /app/tests
USER bot:bot
ENV COVERAGE_FILE=/tmp/.coverage RUFF_CACHE_DIR=/tmp/ruff-cache
RUN python -m pip check && \
    python -m ruff check . && \
    python -m ruff format --check . && \
    python -W error::ResourceWarning -m coverage run -m unittest discover -s tests -q && \
    python -m coverage report

FROM app AS runtime
