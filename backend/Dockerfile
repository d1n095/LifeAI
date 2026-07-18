FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x docker-entrypoint.sh

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
