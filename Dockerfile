FROM python:3.12.13-slim-trixie@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 CORVUS_LISTEN_HOST=0.0.0.0
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 workbench \
    && useradd --uid 10001 --gid workbench --home-dir /app --create-home workbench
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/corvus-venv UV_PYTHON_DOWNLOADS=never PATH=/opt/corvus-venv/bin:$PATH
COPY pyproject.toml uv.lock ./
COPY LICENSE COPYRIGHT THIRD_PARTY_NOTICES.md ./
COPY LICENSES ./LICENSES
COPY CHANGELOG.md ./CHANGELOG.md
COPY docs/MANUAL.md ./docs/MANUAL.md
COPY app ./app
COPY upstreams.lock.json ./
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts/start.py ./scripts/start.py
COPY scripts/materialise_corvus_brand.py ./scripts/materialise_corvus_brand.py
COPY scripts/download_face_models.py ./scripts/download_face_models.py
RUN python scripts/materialise_corvus_brand.py \
    && uv sync --locked --no-dev --no-editable --no-cache \
    && /opt/corvus-venv/bin/python scripts/download_face_models.py \
    && mkdir -p /data \
    && chown -R workbench:workbench /app /data /app/models
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=3s --start-period=10s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
CMD ["python", "scripts/start.py"]
