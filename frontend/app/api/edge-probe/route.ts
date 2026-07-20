import { NextRequest } from "next/server";

// Diagnostic-only route, added 2026-07-20 during the "Render shows Live + /api/health 200 but
// the public root URL 502s" investigation (see docs/RENDER_DEPLOY.md). Unlike every other
// route under /api/*, this one is NOT proxied to the backend (frontend/app/api/[...path]/
// route.ts's catch-all only matches when no more specific literal route exists — Next.js
// always prefers a static segment like this one over the dynamic [...path] catch-all for an
// exact path match). Answers entirely inside this Next.js process: no backend call, no
// database, no Redis. A request that reaches this route and gets a response proves the
// public $PORT is genuinely routed to THIS container's Next.js process, independent of
// whether FastAPI/Postgres/Redis are healthy.
//
// See also /api/edge-health (a second, separate diagnostic path — deliberately not the same
// URL as Render's real healthCheckPath or entrypoint-combined.sh's internal readiness-gate
// hit, so log lines from a manual external check here can never be confused with either) and
// frontend/public/edge-probe.html (a static file with no server code at all, isolating
// "static file serving" from "route handler execution").
//
// Logs only a fixed safelist of headers that are never secrets (Host, X-Forwarded-Proto,
// X-Forwarded-For), the request path, and this process's PID. Never logs Cookie,
// Authorization, or any other credential-bearing header — deliberate, not an oversight.

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const host = request.headers.get("host") ?? "(none)";
  const proto = request.headers.get("x-forwarded-proto") ?? "(none)";
  const forwardedFor = request.headers.get("x-forwarded-for") ?? "(none)";
  console.log(
    `[edge-probe] pid=${process.pid} path=${request.nextUrl.pathname} ` +
      `host=${host} x-forwarded-proto=${proto} x-forwarded-for=${forwardedFor}`
  );
  return new Response(`edge-probe-ok pid=${process.pid}\n`, {
    status: 200,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "x-edge-probe": "1",
      "x-edge-probe-pid": String(process.pid),
      "cache-control": "no-store",
    },
  });
}
