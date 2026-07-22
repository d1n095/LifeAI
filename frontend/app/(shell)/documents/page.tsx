"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

// Life Library upload consolidation package (DEL 1): /library is now the sole upload
// location — this route only ever redirects there. No document or database row was
// touched, so anything uploaded through the old /documents flow keeps appearing via Life
// Library (both routers already read the same underlying `documents` table).
export default function DocumentsRedirectPage() {
  const router = useRouter();

  useEffect(() => {
    router.replace("/library");
  }, [router]);

  return null;
}
