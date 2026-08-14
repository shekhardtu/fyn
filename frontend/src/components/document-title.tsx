import { useEffect } from "react";

const DEFAULT_TITLE = "fyn AI";

/** Keeps document metadata close to the route or data that owns it. */
export function DocumentTitle({ title, fallback = DEFAULT_TITLE }: { title?: string; fallback?: string }) {
  const pageTitle = title?.trim() || fallback;

  useEffect(() => {
    document.title = pageTitle;
  }, [pageTitle]);

  return null;
}
