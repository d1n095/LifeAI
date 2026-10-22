import { AsyncLocalStorage } from "node:async_hooks";
import http from "node:http";
import https from "node:https";

/**
 * Trusted TCP peer for the Next.js hop.
 *
 * `NextRequest.ip` is undefined under self-hosted `next start` / standalone
 * `server.js`. Client `X-Forwarded-For` is never identity. The Node socket
 * remote address is captured once per request and stored in async context.
 */
const peerStore = new AsyncLocalStorage<string>();

export const TRUSTED_PEER_HEADER = "x-mainai-trusted-peer";

let installed = false;

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
