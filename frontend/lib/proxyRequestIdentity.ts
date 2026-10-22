import {
  TRUSTED_PEER_HEADER,
  currentTrustedPeer,
  isPlausiblePeerIp,
  normalizePeerIp,
  peerCaptureInstalled,
} from "./trustedPeerAddress.ts";

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
  // Next's connection IP when the platform actually populated it — never X-Forwarded-For.
  const fromNext = normalizePeerIp(typeof request.ip === "string" ? request.ip : "");
  if (fromNext) return fromNext;
  // Self-hosted `next start` / standalone: request.ip is undefined. Use the TCP peer
  // captured from the Node socket (instrumentation), not a client-supplied header.
  const fromPeer = currentTrustedPeer();
  if (fromPeer) return fromPeer;
  // Instrumentation overwrites this header from the Node socket. Trust it only
  // after that capture is installed — a client-supplied copy is ignored in tests
  // and any runtime that never stamped the TCP peer.
  if (peerCaptureInstalled()) {
    const stamped = normalizePeerIp(request.headers.get(TRUSTED_PEER_HEADER));
    if (stamped) return stamped;
  }
  return "unknown";
}

export function applyTrustedForwardingHeaders(headers: Headers, clientIp: string): Headers {
  for (const name of CLIENT_FORGED_FORWARD_HEADERS) {
    headers.delete(name);
  }
  headers.delete(TRUSTED_PEER_HEADER);
  const identity = isPlausiblePeerIp(clientIp.trim()) ? clientIp.trim() : "unknown";
  headers.set("x-forwarded-for", identity);
  headers.set("x-mainai-client-ip", identity);
  return headers;
}
