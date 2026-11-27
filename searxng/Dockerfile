FROM ghcr.io/searxng/searxng:2026.7.28-c01178d03@sha256:5d6d903ab82afa56ee32792d477f36bc63d3e5ca04fcb6947e28a5cfd987fad3

USER root
RUN --mount=from=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa,source=/uv,target=/usr/local/bin/uv \
    uv pip install --python /usr/local/searxng/.venv/bin/python --no-cache --no-deps certifi==2026.10.18
COPY --chown=977:977 searxng/settings.yml /opt/osint-workbench-searxng/settings.yml
COPY --chown=977:977 searxng/limiter.toml /opt/osint-workbench-searxng/limiter.toml
COPY --chown=977:977 searxng/bing.py /usr/local/searxng/searx/engines/bing_corvus.py
COPY --chown=977:977 searxng/bing_relevance.py /usr/local/searxng/searx/engines/bing_corvus_relevance.py
COPY LICENSES/AGPL-3.0.txt /usr/share/licenses/searxng/AGPL-3.0.txt
COPY --chmod=0755 searxng/entrypoint.sh /usr/local/bin/osint-searxng-entrypoint
USER 977:977
ENTRYPOINT ["/usr/local/bin/osint-searxng-entrypoint"]
