import { AsyncLocalStorage } from "node:async_hooks";
import http from "node:http";
import https from "node:https";

const CLIENT_FORGED_FORWARD_HEADERS = new Set([
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-proto",
  "x-real-ip",
  "forwarded",
]);

export const TRUSTED_PEER_HEADER = "x-mainai-trusted-peer";

const peerStore = new AsyncLocalStorage<string>();
let installed = false;

export function isClientForgedForwardHeader(name: string): boolean {
  return CLIENT_FORGED_FORWARD_HEADERS.has(name.toLowerCase());
}

export function normalizePeerIp(value: string | null | undefined): string {
  let text = (value || "").trim();
  if (!text) return "";
  if (text.startsWith("[") && text.endsWith("]")) text = text.slice(1, -1);
  if (text.toLowerCase().startsWith("::ffff:")) text = text.slice(7);
  if (!isPlausiblePeerIp(text)) return "";
  return text;
}

export function isPlausiblePeerIp(value: string): boolean {
  const text = (value || "").trim();
  if (!text) return false;
  if (/^(unknown|undefined)$/i.test(text)) return false;
  if (/[\s,;/\0]/.test(text)) return false;
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(text)) {
    return text.split(".").every((part) => {
      const n = Number(part);
      return n >= 0 && n <= 255;
    });
  }
  return text.includes(":");
}

export function currentTrustedPeer(): string {
  return normalizePeerIp(peerStore.getStore());
}

export function runWithTrustedPeer<T>(peer: string, fn: () => T): T {
  return peerStore.run(normalizePeerIp(peer) || "unknown", fn);
}

export function peerCaptureInstalled(): boolean {
  return installed;
}

function stampFromSocket(req: http.IncomingMessage): string {
  const peer = normalizePeerIp(req.socket?.remoteAddress);
  const headers = req.headers as http.IncomingHttpHeaders & Record<string, string | string[] | undefined>;
  delete headers[TRUSTED_PEER_HEADER];
  delete headers["X-MainAI-Trusted-Peer"];
  if (peer) headers[TRUSTED_PEER_HEADER] = peer;
  return peer;
}

export function installTrustedPeerCapture(): void {
  if (installed) return;
  installed = true;

  const wrap = (proto: typeof http.Server.prototype) => {
    const original = proto.emit;
    proto.emit = function (event: string | symbol, ...args: unknown[]) {
      if (event === "request" && args[0] && typeof args[0] === "object") {
        const peer = stampFromSocket(args[0] as http.IncomingMessage);
        if (peer) {
          return peerStore.run(peer, () => original.apply(this, [event, ...args] as never));
        }
      }
      return original.apply(this, [event, ...args] as never);
    };
  };

  wrap(http.Server.prototype);
  wrap(https.Server.prototype as unknown as typeof http.Server.prototype);
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
