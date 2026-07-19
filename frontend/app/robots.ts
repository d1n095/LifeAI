import type { MetadataRoute } from "next";

// Extra layer only, not authentication — see backend/app/deps.py's require_founder() for the
// real access control every protected route enforces server-side regardless of this file.
// A crawler that ignores robots.txt entirely still can't reach anything: it just hits the
// same login wall a browser would.
export default function robots(): MetadataRoute.Robots {
  return {
    rules: {
      userAgent: "*",
      disallow: "/",
    },
  };
}
