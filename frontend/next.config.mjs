/** @type {import('next').NextConfig} */
const nextConfig = {
  // "standalone" bundles a self-contained server.js for our own Docker deployment
  // (frontend/Dockerfile copies .next/standalone). Vercel has its own serverless build
  // pipeline that does not run that server.js — shipping "standalone" output there results
  // in a build that reports success but returns NOT_FOUND on every route, including "/".
  // Vercel sets the VERCEL env var automatically during its builds, so this keeps Docker
  // working while letting Vercel use its normal (non-standalone) output.
  output: process.env.VERCEL ? undefined : "standalone",

  // Dev-mode hydration: `next dev --hostname 0.0.0.0` viewed through a non-localhost origin
  // (a preview/sandbox host, e.g. *.style.dev) blocks the cross-origin /_next/webpack-hmr
  // request. React's hydrateRoot() then never resolves its initial server response, so the
  // page renders but stays completely unhydrated: no event handlers, AuthGuard's client
  // createClient() never runs, and the browser console reports the blocked dev resource.
  // Naming the dev origin here is Next's supported fix (Next 15+/16 `allowedDevOrigins`).
  // Comma-separated, empty by default, and scoped to `next dev` only — `next build`/`next
  // start` ignore it, so production behaviour is unchanged.
  allowedDevOrigins: (process.env.NEXT_ALLOWED_DEV_ORIGINS || "")
    .split(",")
    .map((origin) => origin.trim())
    .filter(Boolean),
};

export default nextConfig;
