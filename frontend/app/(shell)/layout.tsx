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
            <Sidebar userEmail={user.email} />
            <main className="flex-1 p-4 md:p-8 min-w-0">{children}</main>
          </div>
        </UploadQueueProvider>
      )}
    </AuthGuard>
  );
}
