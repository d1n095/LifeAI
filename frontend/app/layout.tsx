import type { Metadata, Viewport } from "next";
import "./globals.css";
import PwaController from "@/components/PwaController";

export const metadata: Metadata = {
  title: "Life OS — MainAI",
  description: "Grundarens Founder AI: kunskap, projekt, dokument och kod på ett ställe.",
  // Extra layer only, not the actual gate — see frontend/app/robots.ts and
  // backend/app/deps.py's require_founder() for the real access control. A search engine
  // that ignores this would still hit a founder-only login wall, not a working app.
  robots: { index: false, follow: false },
  // PWA: Next serves the manifest at /manifest.webmanifest (see app/manifest.ts).
  manifest: "/manifest.webmanifest",
  // iOS / macOS Safari "Add to Dock" standalone behavior + title.
  appleWebApp: { capable: true, title: "MainAI", statusBarStyle: "black-translucent" },
  icons: {
    icon: "/icon.svg",
    apple: "/icons/apple-touch-icon.png",
  },
};

export const viewport: Viewport = {
  themeColor: "#05050a",
  // Standalone app windows feel native with no accidental zoom; still accessible-zoomable.
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="sv">
      <body className="min-h-screen bg-base text-white">
        {children}
        <PwaController />
      </body>
    </html>
  );
}
