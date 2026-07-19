import { redirect } from "next/navigation";

// MainAI is Founder-only — there is no self-registration flow (see
// backend/app/routers/auth.py's register(), which 404s in production regardless of this
// redirect; this page's only job is to make sure nothing in the UI ever renders a form that
// could reach it). Anyone who lands here — an old bookmark, a stale link, a crawler — goes
// straight to /login instead of seeing a dead feature.
export default function RegisterPage() {
  redirect("/login");
}
