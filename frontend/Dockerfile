FROM node:20-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:20-slim AS builder
WORKDIR /app
# NEXT_PUBLIC_* variables are inlined into the client bundle at build time, not read at
# container start — setting NEXT_PUBLIC_API_URL only in the runner stage (as a plain runtime
# ENV) would silently have no effect on what the browser actually calls. It must be present
# here, before `npm run build`, if it's set at all.
#
# Empty by default — see frontend/lib/api.ts: an empty value makes the browser call this same
# server's own /api/* proxy route (app/api/[...path]/route.ts), which forwards to the real
# backend using INTERNAL_API_URL, a plain runtime env var read server-side, not baked in here
# and never shipped to the client. Only pass --build-arg NEXT_PUBLIC_API_URL=... for a
# deployment that deliberately calls the backend directly, cross-origin, instead of through
# the proxy (see lib/api.ts's comment on that escape hatch).
ARG NEXT_PUBLIC_API_URL=
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
# Never phone home to Vercel's telemetry collector from a build running in our own CI/VPS.
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:20-slim AS runner

LABEL org.opencontainers.image.title="LifeAI/MainAI frontend" \
      org.opencontainers.image.description="Next.js standalone server — the only service besides Caddy on the private Docker network; proxies /api/* to the backend server-side. See docs/VPS_ARCHITECTURE.md." \
      org.opencontainers.image.source="https://github.com/d1n095/LifeAI" \
      org.opencontainers.image.licenses="UNLICENSED"

WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

# Same rationale as backend/Dockerfile: a dedicated, unprivileged, non-login user. Ownership
# is set via `--chown` on the COPY steps below instead of a separate `chown -R` pass, since
# the builder stage already laid these files down read-only-from-the-app's-perspective.
RUN groupadd --system --gid 10001 nextjs \
    && useradd --system --uid 10001 --gid nextjs --home-dir /app --no-create-home --shell /usr/sbin/nologin nextjs

COPY --from=builder --chown=nextjs:nextjs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nextjs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nextjs /app/public ./public

USER nextjs

EXPOSE 3000
CMD ["node", "server.js"]
