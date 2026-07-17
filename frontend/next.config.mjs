/** @type {import('next').NextConfig} */
const nextConfig = {
  // "standalone" bundles a self-contained server.js for our own Docker deployment
  // (frontend/Dockerfile copies .next/standalone). Vercel has its own serverless build
  // pipeline that does not run that server.js — shipping "standalone" output there results
  // in a build that reports success but returns NOT_FOUND on every route, including "/".
  // Vercel sets the VERCEL env var automatically during its builds, so this keeps Docker
  // working while letting Vercel use its normal (non-standalone) output.
  output: process.env.VERCEL ? undefined : "standalone",
};

export default nextConfig;
