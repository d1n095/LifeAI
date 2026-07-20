import { NextRequest } from "next/server";

// A second, deliberately SEPARATE diagnostic path from /api/edge-probe — same purpose (prove
// the public $PORT reaches this Next.js process, no backend/DB/Redis involved), different URL,
// so a manual external check against this exact path can never be confused in the container
// log with:
// - /api/health — Render's real healthCheckPath (render.yaml), proxied through to FastAPI.
// - /api/health?probe=internal-startup-gate — entrypoint-combined.sh's own internal readiness
//   gate, which also hits /api/health, distinguished only by that query string.
// This route uses neither URL and neither mechanism. See /api/edge-probe/route.ts for the
// fuller rationale and docs/RENDER_DEPLOY.md for the investigation this is part of.
//
// Same logging rule as /api/edge-probe: only Host, X-Forwarded-Proto, X-Forwarded-For, the
// request path, and this process's PID — never Cookie, Authorization, or any other
// credential-bearing header.

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const host = request.headers.get("host") ?? "(none)";
  const proto = request.headers.get("x-forwarded-proto") ?? "(none)";
  const forwardedFor = request.headers.get("x-forwarded-for") ?? "(none)";
  console.log(
    `[edge-health] pid=${process.pid} path=${request.nextUrl.pathname} ` +
      `host=${host} x-forwarded-proto=${proto} x-forwarded-for=${forwardedFor}`
  );
  return new Response("edge-health-ok\n", {
    status: 200,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "x-edge-health": "1",
      "cache-control": "no-store",
    },
  });
}
