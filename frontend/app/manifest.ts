import type { MetadataRoute } from "next";

// Served by Next at /manifest.webmanifest (linked from app/layout.tsx metadata). Makes MainAI
// installable as a standalone PWA on macOS (Chrome/Edge/Safari "Add to Dock") and Android
// (Chrome on the Pixel). Colors match the dark theme (tailwind `base`).
export default function manifest(): MetadataRoute.Manifest {
  return {
    id: "/",
    name: "MainAI",
    short_name: "MainAI",
    description:
      "Grundarens Founder AI: kunskap, projekt, dokument och kod på ett ställe.",
    start_url: "/",
    scope: "/",
    display: "standalone",
    orientation: "any",
    background_color: "#05050a",
    theme_color: "#05050a",
    lang: "sv",
    icons: [
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
      {
        src: "/icons/icon-maskable-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "maskable",
      },
    ],
  };
}
