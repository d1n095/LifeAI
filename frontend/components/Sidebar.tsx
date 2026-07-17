"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV = [
  { href: "/", label: "Dashboard" },
  { href: "/chat", label: "Chat" },
  { href: "/knowledge", label: "Kunskapsdatabas" },
  { href: "/projects", label: "Projekt" },
  { href: "/documents", label: "Dokument" },
  { href: "/admin", label: "Admin" },
];

export default function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="w-56 shrink-0 border-r border-border bg-panel/60 p-4 flex flex-col gap-1">
      <div className="mb-6 px-2">
        <div className="text-sm uppercase tracking-widest text-accent2">Life OS</div>
        <div className="text-xs text-white/40">MainAI v0.1.0</div>
      </div>
      {NAV.map((item) => {
        const active = pathname === item.href;
        return (
          <Link
            key={item.href}
            href={item.href}
            className={`rounded-lg px-3 py-2 text-sm transition-colors ${
              active ? "bg-accent/20 text-white" : "text-white/60 hover:bg-white/5 hover:text-white"
            }`}
          >
            {item.label}
          </Link>
        );
      })}
    </aside>
  );
}
