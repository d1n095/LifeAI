import { NextRequest } from "next/server";

// Diagnostic-only route, part of the "Render shows Live + /api/health 200 but the public root
// URL 502s" investigation — see docs/RENDER_DEPLOY.md's "Ett fjärde, ännu olöst fall". Unlike
// every other route under /api/*, this one is NOT proxied to the backend
// (frontend/app/api/[...path]/route.ts's catch-all only matches when no more specific literal
// route exists — Next.js always prefers a static segment like this one over the dynamic
// [...path] catch-all for an exact path match, verified locally). Answers entirely inside this
// Next.js process: no backend call, no database, no Redis, no AI, no middleware, no change to
// Next's own startup flow. A request that reaches this route and gets a response proves the
// public $PORT is genuinely routed to THIS container's Next.js process, independent of
// whether FastAPI/Postgres/Redis are healthy.
//
// Deliberately NOT a general-purpose request logger — logs only successful (valid probe_id)
// calls to this exact route, and only a fixed, minimal field set. See sanitizeForLog and the
// probe_id validation below for exactly what is and is never logged.

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Computed once, when this module is first loaded by the Node process, and reused for every
// request that process ever handles. Lets a manual test tell "the same container instance
// answered every probe" apart from "a different instance answered this one" (e.g. across a
// Render cutover) purely from the response header, without a custom server or any change to
// how Next.js itself starts up — this is an ordinary module-level constant.
const BOOT_ID = crypto.randomUUID();

const PROBE_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;
const MAX_LOG_FIELD_LENGTH = 255;

// Strips C0 control characters and DEL (this range includes CR 0x0D and LF 0x0A) and caps
// length, so a hostile Host or X-Forwarded-Proto header can never inject extra log lines or
// break the single-line structured format below. probe_id itself never needs this — the regex
// above already restricts it to a safe character set before it's ever used.
function sanitizeForLog(value: string): string {
  return value.replace(/[\x00-\x1F\x7F]/g, "").slice(0, MAX_LOG_FIELD_LENGTH);
}

export async function GET(request: NextRequest) {
  const probeId = request.nextUrl.searchParams.get("probe_id");

  // An invalid probe_id is attacker-controlled, arbitrary-length, arbitrary-content input —
  // exactly what log-injection defenses exist for. It is never echoed in the response and
  // never logged, in any form, valid or not.
  if (!probeId || !PROBE_ID_PATTERN.test(probeId)) {
    return new Response(
      "Bad Request: probe_id query parameter is required and must match ^[A-Za-z0-9_-]{1,64}$\n",
      {
        status: 400,
        headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
      }
    );
  }

  const host = sanitizeForLog(request.headers.get("host") ?? "");
  const proto = sanitizeForLog(request.headers.get("x-forwarded-proto") ?? "");
  // Presence only, never the value — an X-Forwarded-For header can carry a real visitor's IP
  // address, which this diagnostic route has no legitimate reason to persist anywhere.
  const forwardedForPresent = request.headers.has("x-forwarded-for");

  console.log(
    `[edge-probe] time=${new Date().toISOString()} boot_id=${BOOT_ID} pid=${process.pid} ` +
      `probe_id=${probeId} host=${host} proto=${proto} forwarded_for_present=${forwardedForPresent}`
  );

  return new Response("LifeAI edge probe OK\n", {
    status: 200,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "no-store",
      "x-lifeai-probe": "edge-v1",
      "x-lifeai-process-id": String(process.pid),
      "x-lifeai-boot-id": BOOT_ID,
      "x-lifeai-probe-id": probeId,
    },
  });
}
