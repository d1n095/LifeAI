"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";

// Grouped by the founder's job, not by backend module: every route below already existed, the
// grouping only makes the console's modular areas (research, knowledge, security, projects)
// findable. No route is duplicated and no capability is implied by a label.
const NAV_SECTIONS: { heading: string; items: { href: string; label: string }[] }[] = [
  { heading: "Konsol", items: [{ href: "/", label: "Founder Console" }, { href: "/chat", label: "Chat" }] },
  {
    heading: "Research",
    items: [
      { href: "/workbench", label: "Workbench" },
      { href: "/blueprint", label: "Blueprint" },
    ],
  },
  {
    heading: "Kunskap",
    items: [
      { href: "/library", label: "Knowledge Studio" },
      { href: "/documents", label: "Dokument" },
      { href: "/knowledge", label: "Kunskapsdatabas" },
    ],
  },
  {
    heading: "Projekt",
    items: [
      { href: "/projects", label: "Projekt" },
      { href: "/admin/memory", label: "Projektminne" },
    ],
  },
  {
    heading: "Säkerhet",
    items: [
      { href: "/admin", label: "Admin" },
      { href: "/admin/agents", label: "Agentuppdrag" },
      { href: "/admin/jobs", label: "Jobb & Aktivitet" },
      { href: "/admin/mainai-execution", label: "MainAI Execution" },
    ],
  },
  { heading: "Konto", items: [{ href: "/account", label: "Konto" }] },
];

export default function Sidebar({ userEmail }: { userEmail: string }) {
  const pathname = usePathname();
  const router = useRouter();
  const [mobileOpen, setMobileOpen] = useState(false);

  async function logout() {
    // Revokes the refresh token server-side and clears all three cookies via Set-Cookie —
    // "logging out" client-side alone would leave a still-valid session on the server.
    try {
      await api.logout();
    } finally {
      router.replace("/login");
    }
  }

  const navLinks = (
    <nav aria-label="Huvudmeny" className="flex flex-col gap-3">
      {NAV_SECTIONS.map((section) => (
        <div key={section.heading}>
          <div className="px-3 pb-1 text-[10px] uppercase tracking-widest text-white/25">{section.heading}</div>
          <div className="flex flex-col gap-0.5">
            {section.items.map((item) => {
              const active = pathname === item.href;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  onClick={() => setMobileOpen(false)}
                  className={`rounded-lg px-3 py-2 text-sm transition-colors ${
                    active ? "bg-accent/20 text-white" : "text-white/60 hover:bg-white/5 hover:text-white"
                  }`}
                >
                  {item.label}
                </Link>
              );
            })}
          </div>
        </div>
      ))}
    </nav>
  );

  return (
    <>
      {/* Mobile top bar */}
      <div className="md:hidden flex items-center justify-between border-b border-border bg-panel/60 px-4 py-3">
        <div className="text-sm uppercase tracking-widest text-accent2">Life OS · MainAI</div>
        <button
          type="button"
          onClick={() => setMobileOpen((v) => !v)}
          aria-expanded={mobileOpen}
          aria-controls="mobile-nav"
          aria-label={mobileOpen ? "Stäng meny" : "Öppna meny"}
          className="rounded-lg border border-border px-3 py-1.5 text-sm"
        >
          {mobileOpen ? "Stäng" : "Meny"}
        </button>
      </div>
      {mobileOpen && (
        <div id="mobile-nav" className="md:hidden border-b border-border bg-panel/95 p-4">
          {navLinks}
          <button
            type="button"
            onClick={logout}
            className="mt-3 w-full rounded-lg border border-border px-3 py-2 text-left text-sm text-white/60 hover:text-white"
          >
            Logga ut ({userEmail})
          </button>
        </div>
      )}

      {/* Desktop sidebar */}
      <aside className="hidden md:flex w-56 shrink-0 border-r border-border bg-panel/60 p-4 flex-col gap-1">
        <div className="mb-6 px-2">
          <div className="text-sm uppercase tracking-widest text-accent2">Life OS</div>
          <div className="text-xs text-white/40">MainAI v0.1.0</div>
        </div>
        {navLinks}
        <div className="mt-auto pt-4 border-t border-border/60">
          <div className="px-2 pb-2 text-xs text-white/40 truncate" title={userEmail}>
            {userEmail}
          </div>
          <button
            type="button"
            onClick={logout}
            className="w-full rounded-lg px-3 py-2 text-left text-sm text-white/60 hover:bg-white/5 hover:text-white"
          >
            Logga ut
          </button>
        </div>
      </aside>
    </>
  );
}
