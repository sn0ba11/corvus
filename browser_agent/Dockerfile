FROM mcr.microsoft.com/playwright/python:v1.61.0-noble@sha256:a9731514f24121d1dcd25d58d0a38146646d290a5998fd80d3e533e7b5e21c69

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends openbox novnc python3-venv websockify x11vnc xdotool xvfb \
    && apt-get purge -y gstreamer1.0-plugins-bad libgstreamer-plugins-bad1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Refresh the Python packages preinstalled in the Playwright base image.
# Install a Python-local packaging wheel without replacing Ubuntu's copy.
RUN python -m pip install --break-system-packages --no-cache-dir --ignore-installed --no-deps packaging==26.4 \
    && python -m pip install --break-system-packages --no-cache-dir filelock==4.2.1 virtualenv==21.14.13 tzdata==2026.6 \
    && python -m pip check \
    && python -m venv /opt/browser-venv \
    && /opt/browser-venv/bin/pip install --no-cache-dir \
       pynacl==1.6.2 fastapi==0.139.2 starlette==1.3.1 playwright==1.61.0 patchright==1.61.2 invisible-playwright==0.6.1 pydantic==2.13.4 uvicorn==0.35.0 filelock==4.2.1 virtualenv==21.14.13 \
    && /opt/browser-venv/bin/pip check \
    && groupadd --gid 10001 browser \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/browser browser \
    && HOME=/home/browser /opt/browser-venv/bin/patchright install chromium \
    && HOME=/home/browser /opt/browser-venv/bin/python -m invisible_playwright fetch \
    && mkdir -p /profile /evidence \
    && chown -R 10001:10001 /profile /evidence /home/browser

WORKDIR /opt/osint-browser
COPY --chown=10001:10001 browser_agent /opt/osint-browser/browser_agent
# Git for Windows may supply CRLF files when an older checkout predates
# .gitattributes. Normalise the entrypoint in the image as a second guard.
RUN sed -i 's/\r$//' /opt/osint-browser/browser_agent/start.sh \
    && chmod 0555 /opt/osint-browser/browser_agent/start.sh

ENV HOME=/home/browser \
    PATH=/opt/browser-venv/bin:$PATH \
    XDG_CONFIG_HOME=/profile/xdg-config \
    XDG_CACHE_HOME=/profile/xdg-cache \
    INVISIBLE_PLAYWRIGHT_CACHE_DIR=/home/browser/.cache/invisible-playwright \
    PYTHONUNBUFFERED=1 \
    BROWSER_PROFILE_DIR=/profile \
    BROWSER_EVIDENCE_DIR=/evidence
USER 10001:10001
EXPOSE 8080 6080
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=5 \
  CMD python -c "import os,urllib.request; r=urllib.request.Request('http://127.0.0.1:8080/healthz',headers={'Authorization':'Bearer '+os.environ['BROWSER_AGENT_TOKEN']}); urllib.request.urlopen(r,timeout=3).read()"
ENTRYPOINT ["/opt/osint-browser/browser_agent/start.sh"]
