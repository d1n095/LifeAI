import { NextRequest } from "next/server";

// Same-origin API proxy. Every browser request under /api/* lands here and is forwarded to
// the real backend — the browser itself never sees or calls the backend's actual address.
// That's what makes the session cookie (HttpOnly, see backend/app/cookies.py) a same-origin
// cookie instead of a cross-origin one: the Set-Cookie header the backend sends back arrives
// at the browser as if it came from this Next.js server, so it's scoped to *this* origin,
// sidestepping SameSite=None / third-party-cookie restrictions entirely regardless of where
// the backend actually runs.
//
// INTERNAL_API_URL is a server-only env var (deliberately NOT prefixed NEXT_PUBLIC_ — see
// frontend/lib/api.ts) — it never reaches the client bundle. On Render it points at the
// backend's private-network address (unreachable from the public internet at all); locally
// it defaults to the backend running directly on localhost.
//
// Forwarding is byte-for-byte, not parsed/re-serialized: method, headers (Cookie in,
// Set-Cookie out — see the getSetCookie() note below), body (streamed, so this works
// unmodified for both JSON and multipart file uploads, and for any future streaming
// response), query string, and status code.

export const runtime = "nodejs";
// Never let Next.js cache or statically optimize this route — every response here can be
// specific to one authenticated user (session cookie), so caching it would risk serving one
// user's data to another.
export const dynamic = "force-dynamic";

const INTERNAL_API_URL = process.env.INTERNAL_API_URL || "http://localhost:8000";

// Hop-by-hop headers (RFC 7230 §6.1) are meaningful for exactly one transport hop and must
// not be blindly relayed to the next one — this proxy IS a hop.
const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "host",
  "content-length", // recomputed by fetch() from the actual streamed body
  // Meaningful for exactly the client->this-hop leg, not this hop's own outbound fetch() —
  // and undici (Node's fetch implementation) throws `NotSupportedError: expect header not
  // supported` if it's forwarded, since Expect is a forbidden header per the Fetch spec.
  // Many HTTP clients (curl included) attach "Expect: 100-continue" automatically once a
  // request body crosses a size threshold, so without stripping this, any sufficiently large
  // POST/PUT/PATCH through this proxy would crash outright — found via
  // vps-compose-verify's oversized-body CI check, not a hypothetical.
  "expect",
]);

function copyHeaders(source: Headers): Headers {
  const dest = new Headers();
  source.forEach((value, key) => {
    if (!HOP_BY_HOP_HEADERS.has(key.toLowerCase())) {
      dest.set(key, value);
    }
  });
  return dest;
}

async function proxy(request: NextRequest, path: string[]): Promise<Response> {
  const targetUrl = new URL(`${INTERNAL_API_URL}/api/${path.join("/")}`);
  targetUrl.search = new URL(request.url).search;

  const hasBody = !["GET", "HEAD"].includes(request.method);

  let backendResponse: Response;
  try {
    backendResponse = await fetch(targetUrl, {
      method: request.method,
      headers: copyHeaders(request.headers),
      body: hasBody ? request.body : undefined,
      // Required by undici whenever the body is a ReadableStream rather than a buffered value.
      // @ts-expect-error -- `duplex` is a real, required fetch option for streamed bodies but
      // missing from the DOM lib's RequestInit type as of this Next.js/TS version.
      duplex: hasBody ? "half" : undefined,
      redirect: "manual",
      cache: "no-store",
    });
  } catch (err) {
    // The backend is unreachable — wrong INTERNAL_API_URL, the backend service doesn't exist
    // yet, it hasn't finished deploying, or it crashed. Left uncaught, this throws out of the
    // Route Handler and Next.js returns a bare 500 with no body — indistinguishable from a
    // real backend error and useless for diagnosing which of those it actually is. Logged
    // server-side (visible in this service's own Render logs, not the backend's) and
    // returned as a distinct, structured response instead.
    console.error(`[api-proxy] could not reach backend at ${targetUrl}:`, err);
    return Response.json(
      { detail: "Kunde inte nå backend-tjänsten.", proxy_error: true },
      { status: 502 }
    );
  }

  const responseHeaders = copyHeaders(backendResponse.headers);
  // Headers.forEach/get collapse multiple Set-Cookie instances into one comma-joined string
  // (fine for every other header, wrong for this one — browsers can't parse that back into
  // separate cookies). getSetCookie() is the one API that exposes the real list; re-append
  // each cookie individually instead of letting copyHeaders() merge them.
  responseHeaders.delete("set-cookie");
  for (const cookie of backendResponse.headers.getSetCookie()) {
    responseHeaders.append("set-cookie", cookie);
  }

  return new Response(backendResponse.body, {
    status: backendResponse.status,
    statusText: backendResponse.statusText,
    headers: responseHeaders,
  });
}

type RouteContext = { params: Promise<{ path: string[] }> };

export async function GET(request: NextRequest, context: RouteContext) {
  return proxy(request, (await context.params).path);
}
export async function POST(request: NextRequest, context: RouteContext) {
  return proxy(request, (await context.params).path);
}
export async function PUT(request: NextRequest, context: RouteContext) {
  return proxy(request, (await context.params).path);
}
export async function PATCH(request: NextRequest, context: RouteContext) {
  return proxy(request, (await context.params).path);
}
export async function DELETE(request: NextRequest, context: RouteContext) {
  return proxy(request, (await context.params).path);
}
export async function OPTIONS(request: NextRequest, context: RouteContext) {
  return proxy(request, (await context.params).path);
}
