const CLIENT_FORGED_FORWARD_HEADERS = new Set([
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-proto",
  "x-real-ip",
  "forwarded",
]);

export function isClientForgedForwardHeader(name: string): boolean {
  return CLIENT_FORGED_FORWARD_HEADERS.has(name.toLowerCase());
}

export function trustedClientIp(request: { ip?: string | null; headers: Headers }): string {
  // Next's connection / platform IP — never a client-supplied X-Forwarded-For.
  const ip = typeof request.ip === "string" ? request.ip.trim() : "";
  if (ip) return ip;
  return "unknown";
}

export function applyTrustedForwardingHeaders(headers: Headers, clientIp: string): Headers {
  for (const name of CLIENT_FORGED_FORWARD_HEADERS) {
    headers.delete(name);
  }
  const identity = clientIp.trim() || "unknown";
  headers.set("x-forwarded-for", identity);
  headers.set("x-mainai-client-ip", identity);
  return headers;
}
