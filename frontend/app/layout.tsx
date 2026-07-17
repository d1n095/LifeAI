import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Life OS — MainAI",
  description: "Företagets centrala AI-hjärna: kunskap, projekt, dokument och kod på ett ställe.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="sv">
      <body className="min-h-screen bg-base text-white">{children}</body>
    </html>
  );
}
