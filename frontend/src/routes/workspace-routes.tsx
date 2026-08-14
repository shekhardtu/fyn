import { Outlet } from "react-router";
import { FynWorkspace, WorkspaceShell } from "@/components/workspace";

export function HomeRoute() {
  return <FynWorkspace />;
}

/** Stable owner of the conversation rail and composer across ID changes. */
export function ConversationLayoutRoute() {
  return <WorkspaceShell><Outlet /></WorkspaceShell>;
}
