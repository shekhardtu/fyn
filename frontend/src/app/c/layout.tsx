import { WorkspaceShell } from "@/components/workspace";

// The whole workspace lives here rather than in the page, and that is load
// bearing: Next replaces the page subtree whenever the [conversationId] segment
// changes, so anything rendered from the page — the header, the composer, the
// scroll container, every mounted widget — was being destroyed and rebuilt for
// what is only a change of contents. A layout is the one place above that
// boundary, so the shell renders the thread itself and switching conversations
// swaps the transcript in place.
//
// The page still exists, because a route needs one; it renders nothing.
export default function ConversationLayout({ children }: LayoutProps<"/c">) {
  return <WorkspaceShell>{children}</WorkspaceShell>;
}
