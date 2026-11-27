FROM python:3.12.13-slim-trixie@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

ARG TOOL_ID
ARG UPSTREAM_REPO
ARG UPSTREAM_COMMIT
ARG WMN_REPO=https://github.com/WebBreacher/WhatsMyName.git
ARG WMN_COMMIT=7c445952f2c40689da8b5f677a591ff341f4a8db
ENV TOOL_ID=${TOOL_ID} UPSTREAM_COMMIT=${UPSTREAM_COMMIT} PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN git clone --filter=blob:none "${UPSTREAM_REPO}" /opt/tool \
 && git -C /opt/tool checkout --detach "${UPSTREAM_COMMIT}" \
 && if [ "${TOOL_ID}" = yt_dlp_metadata ]; then \
      sed -i '/^from \.shahid import (/,/^)/d' /opt/tool/yt_dlp/extractor/_extractors.py \
      && rm /opt/tool/yt_dlp/extractor/shahid.py; \
    fi \
 && rm -rf /opt/tool/.git
WORKDIR /opt/tool
COPY tool_containers/instaloader_probe.py /usr/local/bin/instaloader-osint
COPY tool_containers/social_analyzer_probe.py /usr/local/bin/social-analyzer-osint
COPY tool_containers/patch_blackbird_output.py /tmp/patch_blackbird_output.py
RUN sed -i 's/\r$//' /usr/local/bin/instaloader-osint /usr/local/bin/social-analyzer-osint \
 && case "${TOOL_ID}" in \
      sherlock|nexfil) pip install --no-cache-dir . ;; \
      maigret) sed -i 's/soupsieve = "\^2.6"/soupsieve = "4.0.0"/' pyproject.toml \
        && pip install --no-cache-dir . \
        && pip check ;; \
      theharvester) sed -i 's/"aiohttp==3.14.1"/"aiohttp==3.14.4"/; s/"certifi==2026.6.17"/"certifi==2026.10.18"/; s/"dnspython==2.8.0"/"dnspython==2.9.2"/' pyproject.toml \
        && pip install --no-cache-dir . \
        && pip check ;; \
      naminter) sed -i '/"weasyprint>=68.1",/d' pyproject.toml \
        && sed -i '/^from weasyprint import HTML/d; /weasyprint_html = HTML(string=html)/i\            from weasyprint import HTML' naminter/cli/exporters.py \
        && pip install --no-cache-dir . \
        && pip check \
        && git clone --filter=blob:none "${WMN_REPO}" /tmp/whatsmyname \
        && git -C /tmp/whatsmyname checkout --detach "${WMN_COMMIT}" \
        && cp /tmp/whatsmyname/wmn-data.json /opt/tool/wmn-data.json \
        && install -D -m 644 /tmp/whatsmyname/LICENSE.md /usr/share/licenses/whatsmyname/LICENSE.md \
        && rm -rf /tmp/whatsmyname ;; \
      social_analyzer) pip install --no-cache-dir -r requirements.txt \
        && chmod 0755 /usr/local/bin/social-analyzer-osint ;; \
      instaloader) pip install --no-cache-dir . PySocks==1.7.1 \
        && chmod 0755 /usr/local/bin/instaloader-osint ;; \
      gallery_dl) pip install --no-cache-dir . PySocks==1.7.1 ;; \
      yt_dlp_metadata) pip install --no-cache-dir . PySocks==1.7.1 curl_cffi==0.15.0 \
        && python -c 'from yt_dlp.extractor import _extractors; assert hasattr(_extractors, "YoutubeIE") and not hasattr(_extractors, "ShahidIE")' ;; \
      holehe) pip install --no-cache-dir . requests==2.34.2 ;; \
      blackbird) sed -i \
          -e 's/^aiohttp==3.12.13$/aiohttp==3.14.4/' \
          -e 's/^aiosignal==1.3.2$/aiosignal==1.4.0/' \
          -e 's/^certifi==2025.6.15$/certifi==2026.10.18/' \
          -e 's/^pillow==11.3.0$/pillow==13.0.0/' \
          -e 's/^urllib3==2.5.0$/urllib3==2.8.0/' requirements.txt \
        && sed -i 's/^yarl==1.20.1$/yarl==1.25.1/' requirements.txt \
        && pip install --no-cache-dir -r requirements.txt \
        && pip check \
        && git clone --filter=blob:none "${WMN_REPO}" /tmp/whatsmyname \
        && git -C /tmp/whatsmyname checkout --detach "${WMN_COMMIT}" \
        && cp /tmp/whatsmyname/wmn-data.json /opt/tool/data/wmn-data.json \
        && install -D -m 644 /tmp/whatsmyname/LICENSE.md /usr/share/licenses/whatsmyname/LICENSE.md \
        && rm -rf /tmp/whatsmyname \
        && python /tmp/patch_blackbird_output.py \
        && printf '#!/bin/sh\nexec python /opt/tool/blackbird.py "$@"\n' > /usr/local/bin/blackbird \
        && chmod 0755 /usr/local/bin/blackbird ;; \
      *) exit 2 ;; \
    esac \
 && groupadd --gid 10001 tool && useradd --uid 10001 --gid tool --home-dir /tmp/tool-home --create-home tool \
 && mkdir -p /output && chown -R tool:tool /output /tmp/tool-home
RUN apt-get purge -y git git-man libcurl3t64-gnutls libexpat1
COPY --chown=tool:tool tool_containers/session_bridge.py /session_bridge.py
COPY --chown=tool:tool tool_containers/runner.py /runner.py
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=20s --timeout=3s --start-period=10s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]
ENTRYPOINT ["python", "/runner.py"]
