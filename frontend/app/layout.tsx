import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Life OS — MainAI",
  description: "Grundarens Founder AI: kunskap, projekt, dokument och kod på ett ställe.",
  // Extra layer only, not the actual gate — see frontend/app/robots.ts and
  // backend/app/deps.py's require_founder() for the real access control. A search engine
  // that ignores this would still hit a founder-only login wall, not a working app.
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="sv">
      <body className="min-h-screen bg-base text-white">{children}</body>
    </html>
  );
}
