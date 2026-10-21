export function isUnsafeProxyPathSegment(segment: string): boolean {
  if (!segment) return true;
  let decoded = segment;
  try {
    decoded = decodeURIComponent(segment);
  } catch {
    return true;
  }
  const normalized = decoded.replace(/\\/g, "/").toLowerCase();
  if (normalized === "." || normalized === "..") return true;
  if (normalized.includes("..") || segment.toLowerCase().includes("%2e")) return true;
  if (normalized.includes("/") || normalized.includes("\0")) return true;
  return false;
}

export function assertSafeProxyPath(path: string[]): string[] {
  if (!Array.isArray(path) || path.length === 0) {
    throw new Error("empty proxy path");
  }
  if (path.some(isUnsafeProxyPathSegment)) {
    throw new Error("unsafe proxy path");
  }
  return path;
}
