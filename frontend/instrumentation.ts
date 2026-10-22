export async function register() {
  if (process.env.NEXT_RUNTIME === "edge") return;
  const { installTrustedPeerCapture } = await import("./lib/proxyRequestIdentity");
  installTrustedPeerCapture();
}
