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
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:20-slim AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

EXPOSE 3000
CMD ["node", "server.js"]
