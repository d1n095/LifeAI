"use client";

import AuthGuard from "@/components/AuthGuard";
import Sidebar from "@/components/Sidebar";

export default function ShellLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGuard>
      {(user) => (
        <div className="flex min-h-screen flex-col md:flex-row">
          <Sidebar userEmail={user.email} />
          <main className="flex-1 p-4 md:p-8 min-w-0">{children}</main>
        </div>
      )}
    </AuthGuard>
  );
}
