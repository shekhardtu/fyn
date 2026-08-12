import { WorkspaceShell } from "@/components/workspace";

// The rail, the scrim and the privacy drawer live here rather than in the page
// so they survive navigation between conversations. Next replaces the page
// subtree whenever the [conversationId] segment changes; a layout is the only
// place above that boundary.
export default function ConversationLayout({ children }: LayoutProps<"/c">) {
  return <WorkspaceShell>{children}</WorkspaceShell>;
}
