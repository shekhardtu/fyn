import type { Metadata } from "next";
import { cookies } from "next/headers";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type ConversationPageProps = {
  params: Promise<{ conversationId: string }>;
};

/** Give Next ownership of the title for this route. The workspace itself is a
 *  persistent client layout, but metadata belongs to the dynamic page segment
 *  so Next updates it as part of every sidebar navigation. */
export async function generateMetadata({ params }: ConversationPageProps): Promise<Metadata> {
  const { conversationId } = await params;

  try {
    const cookieHeader = (await cookies()).toString();
    const response = await fetch(`${API_URL}/api/conversations/${encodeURIComponent(conversationId)}`, {
      cache: "no-store",
      headers: cookieHeader ? { Cookie: cookieHeader } : undefined,
    });
    if (!response.ok) return {};

    const conversation: unknown = await response.json();
    const title = conversation && typeof conversation === "object" && typeof (conversation as { title?: unknown }).title === "string"
      ? (conversation as { title: string }).title.trim()
      : "";
    return title ? { title } : {};
  } catch {
    // The thread view owns its visible error state. Metadata failure must not
    // turn a recoverable API outage into a failed route render.
    return {};
  }
}

// The workspace is rendered by `app/c/layout.tsx`, which sits above the segment
// boundary and therefore survives navigation between conversations. This file
// exists so the route resolves; rendering the thread from here would put it
// back inside the subtree Next replaces on every switch.
export default function ConversationPage() {
  return null;
}
