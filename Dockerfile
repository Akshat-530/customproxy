# syntax=docker/dockerfile:1
# ============================================================================
# CustomProxy - OpenAI-compatible aggregator + optional key-rotation relays
#   CustomProxy  :3001                       OpenAI-compatible aggregator
#   Relays       :4016 :4017 :4019 :4021 :4022 :4030  (opt-in, one per MCP)
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
COPY healthcheck.py /app/healthcheck.py
COPY entrypoint.sh /usr/local/bin/customproxy-entrypoint.sh
COPY run-services.sh /usr/local/bin/customproxy-services.sh

RUN chmod +x /usr/local/bin/customproxy-entrypoint.sh \
             /usr/local/bin/customproxy-services.sh \
 && chown -R app:app /app

# Writable mounts. Declared so a bare `docker run` (no compose) still works,
# and pointed at explicitly so the image layout is independent of where the
# project was cloned.
RUN mkdir -p /state /logs && chown -R app:app /state /logs
VOLUME ["/state", "/logs"]

ENV KILO_CONFIG_PATH=/state/kilo_models.json \
    REQUEST_LOG_JSONL=/logs/requests.jsonl

EXPOSE 3001 4016 4017 4019 4021 4022 4030

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python3", "/app/healthcheck.py"]

LABEL org.opencontainers.image.title="customproxy" \
      org.opencontainers.image.description="CustomProxy - OpenAI-compatible multi-provider aggregator with key rotation" \
      org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["/bin/sh", "/usr/local/bin/customproxy-entrypoint.sh"]
