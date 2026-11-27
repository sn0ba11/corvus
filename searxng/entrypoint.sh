#!/bin/sh
set -eu

install -d -m 0755 /etc/searxng /var/cache/searxng
install -m 0644 /opt/osint-workbench-searxng/settings.yml /etc/searxng/settings.yml
install -m 0644 /opt/osint-workbench-searxng/limiter.toml /etc/searxng/limiter.toml

exec /usr/local/searxng/.venv/bin/granian searx.webapp:app
