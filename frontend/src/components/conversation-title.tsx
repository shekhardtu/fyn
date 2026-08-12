"use client";

import { useEffect } from "react";

const DEFAULT_CONVERSATION_TITLE = "Financial check-in";

export function ConversationTitle({ title }: { title: string }) {
  const pageTitle = title.trim() || DEFAULT_CONVERSATION_TITLE;

  // Route changes are owned by generateMetadata. This client-side update also
  // covers a title changing without navigation, such as the first message
  // renaming a newly created conversation.
  useEffect(() => {
    document.title = pageTitle;
  }, [pageTitle]);

  return null;
}
