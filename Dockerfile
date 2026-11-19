# syntax=docker/dockerfile:1
# ============================================================================
# Model_Providers - CustomProxy + Key-Rotation Relays
#   CustomProxy  :3001                       OpenAI-compatible aggregator
#   Relays       :4016 :4017 :4019 :4021 :4022 :4030
# ============================================================================
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies are pinned in requirements.txt; a failure fails the build.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm -f /tmp/requirements.txt

# util-linux provides setpriv (used by the entrypoint to drop privileges).
RUN apt-get update \
 && apt-get install -y --no-install-recommends util-linux \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user/group (deterministic ids).
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

COPY src/ /app/customproxy/
COPY key_rotate_proxy.py /app/key_rotate_proxy.py
COPY entrypoint.sh /usr/local/bin/model-providers-entrypoint.sh
COPY run-services.sh /usr/local/bin/model-providers-services.sh

RUN chmod +x /usr/local/bin/model-providers-entrypoint.sh \
             /usr/local/bin/model-providers-services.sh \
 && chown -R app:app /app

EXPOSE 3001 4016 4017 4019 4021 4022 4030

# Standalone healthcheck (compose overrides it with the full relay+proxy check).
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python3 -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3001/health',timeout=5).status==200 else 1)"

LABEL org.opencontainers.image.title="model-providers" \
      org.opencontainers.image.description="CustomProxy OpenAI-compatible aggregator + key-rotation relays" \
      org.opencontainers.image.source="local-build" \
      org.opencontainers.image.url="" \
      org.opencontainers.image.version="1.0" \
      org.opencontainers.image.revision="" \
      org.opencontainers.image.created="2026-11-19T00:00:00Z" \
      org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["/bin/sh", "/usr/local/bin/model-providers-entrypoint.sh"]

