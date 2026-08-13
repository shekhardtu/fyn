import type { Metadata } from "next";
import { OverviewPage } from "@/components/overview";
import { WorkspaceShell } from "@/components/workspace";

export const metadata: Metadata = {
  title: "Overview",
};

export default function OverviewRoute() {
  return <WorkspaceShell><OverviewPage /></WorkspaceShell>;
}
