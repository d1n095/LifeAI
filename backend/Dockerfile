FROM python:3.12-slim

LABEL org.opencontainers.image.title="LifeAI/MainAI backend" \
      org.opencontainers.image.description="FastAPI backend — deployed with no public ingress, reachable only via the frontend's proxy route on a private Docker network. See docs/VPS_ARCHITECTURE.md." \
      org.opencontainers.image.source="https://github.com/d1n095/LifeAI" \
      org.opencontainers.image.licenses="UNLICENSED"

WORKDIR /app

# curl is the only apt dependency actually needed at runtime (Docker HEALTHCHECK — see
# docker-compose.vps.yml). build-essential/libpq-dev were removed: every package in
# requirements.txt (including psycopg2-binary, which statically bundles libpq) installs from
# a prebuilt manylinux wheel — verified by installing this exact requirements.txt with no C
# compiler present at all. Fewer packages in the runtime image means less attack surface and
# a smaller, faster-to-pull image, not just a cosmetic cleanup.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# PYTHONDONTWRITEBYTECODE: no .pyc files written into the (potentially read-only, see
# docker-compose.vps.yml's read_only:true) application directory at import time.
# PYTHONUNBUFFERED: stdout/stderr are flushed immediately instead of being block-buffered —
# without this, log lines can sit in an internal buffer and never reach `docker logs` /
# the json-file log driver until the buffer fills or the process exits, which is especially
# misleading for a container that's supposed to be observable in real time.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Dedicated, unprivileged, non-login system user — this container never needs a real shell
# login and never needs to be root for anything it actually does (bind ports 8000, connect
# out to Postgres/Redis, read its own files, write to /tmp for scripts/ensure_app_role.py's
# mktemp handoff file). Running as root inside the container buys nothing here and only
# matters if a future vulnerability in a dependency ever allows code execution — root inside
# a container is not a full sandbox escape by itself, but it removes one layer an attacker
# would otherwise have to cross first.
RUN groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid appuser --home-dir /app --no-create-home --shell /usr/sbin/nologin appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x docker-entrypoint.sh \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
# --proxy-headers --forwarded-allow-ips=* makes uvicorn trust X-Forwarded-For from whatever
# connects to it and use that as request.client.host (what app/limiter.py's rate limiting,
# app/audit.py and app/routers/auth.py's login IP logging all key on) instead of the raw TCP
# peer address. Trusting ANY peer like this is only safe because this container is meant to
# be deployed with no public ingress at all (a Render Private Service, or an internal-only
# network in other setups) — the only thing that can ever connect is the trusted frontend
# proxy (frontend/app/api/[...path]/route.ts), which sets X-Forwarded-For from the real
# client IP it received. Never expose this container's port directly to the public internet
# with this flag set — see docs/RENDER_DEPLOY.md.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
