FROM debian:trixie-slim@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0

RUN apt-get update && apt-get upgrade -y --no-install-recommends && apt-get install -y --no-install-recommends tor \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10002 torcorvus \
 && useradd --uid 10002 --gid 10002 --home-dir /var/lib/tor --no-create-home torcorvus \
 && mkdir -p /var/lib/tor \
 && chown -R torcorvus:torcorvus /var/lib/tor /etc/tor \
 && chmod 0700 /var/lib/tor

COPY --chown=torcorvus:torcorvus torproxy/torrc /etc/tor/torrc

USER torcorvus
EXPOSE 9050
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=8 \
  CMD ["bash", "-c", "echo > /dev/tcp/127.0.0.1/9050"]
ENTRYPOINT ["/usr/bin/tor", "-f", "/etc/tor/torrc"]
