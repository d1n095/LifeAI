"use client";

import AuthGuard from "@/components/AuthGuard";
import Sidebar from "@/components/Sidebar";
import { UploadQueueProvider } from "@/lib/uploadQueue";

export default function ShellLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGuard>
      {(user) => (
        // UploadQueueProvider lives here, above the routed page — its state (including any
        // in-flight uploads) survives navigating between shell pages, since only `children`
        // remounts on navigation, not this layout. See lib/uploadQueue.tsx.
        <UploadQueueProvider>
          <div className="flex min-h-screen flex-col md:flex-row">
            <a
              href="#main-content"
              className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:m-2 focus:rounded-lg focus:bg-panel focus:px-3 focus:py-2 focus:text-sm"
            >
              Hoppa till innehållet
            </a>
            <Sidebar userEmail={user.email} userRole={user.role} />
            <main id="main-content" className="flex-1 p-4 md:p-8 min-w-0" tabIndex={-1}>
              {children}
            </main>
          </div>
        </UploadQueueProvider>
      )}
    </AuthGuard>
  );
}
