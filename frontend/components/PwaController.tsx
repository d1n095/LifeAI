"use client";

// Registers the service worker and offers a native install affordance when the browser
// signals the app is installable (Chrome/Edge on Android + desktop fire `beforeinstallprompt`;
// Safari/macOS use "Add to Dock" from the browser UI instead, so no button is shown there).
// Renders nothing until the browser says install is available, and nothing once installed.

import { useEffect, useState } from "react";

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

export default function PwaController() {
  const [deferred, setDeferred] = useState<BeforeInstallPromptEvent | null>(null);
  const [hidden, setHidden] = useState(true);

  useEffect(() => {
    if (
      typeof navigator !== "undefined" &&
      "serviceWorker" in navigator &&
      process.env.NODE_ENV === "production"
    ) {
      navigator.serviceWorker.register("/sw.js").catch(() => {
        /* SW is a progressive enhancement; failure must never break the app */
      });
    }

    const alreadyStandalone =
      typeof window !== "undefined" &&
      window.matchMedia?.("(display-mode: standalone)").matches;

    const onPrompt = (e: Event) => {
      e.preventDefault(); // keep our own affordance instead of the mini-infobar
      setDeferred(e as BeforeInstallPromptEvent);
      if (!alreadyStandalone) setHidden(false);
    };
    const onInstalled = () => {
      setHidden(true);
      setDeferred(null);
    };

    window.addEventListener("beforeinstallprompt", onPrompt);
    window.addEventListener("appinstalled", onInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", onPrompt);
      window.removeEventListener("appinstalled", onInstalled);
    };
  }, []);

  if (hidden || !deferred) return null;

  return (
    <button
      type="button"
      onClick={async () => {
        const e = deferred;
        setDeferred(null);
        setHidden(true);
        await e.prompt();
        await e.userChoice;
      }}
      className="fixed bottom-4 right-4 z-50 rounded-full bg-accent px-4 py-2 text-sm font-medium text-white shadow-lg transition hover:opacity-90"
      aria-label="Installera MainAI som app"
    >
      Installera MainAI
    </button>
  );
}
