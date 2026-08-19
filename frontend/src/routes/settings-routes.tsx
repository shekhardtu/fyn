import { useQueryClient } from "@tanstack/react-query";
import { Navigate, useNavigate } from "react-router";
import { DocumentTitle } from "@/components/document-title";
import { ProfilePanel } from "@/components/profile";
import { SettingsPage } from "@/components/settings";
import { AgentSettingsPanel } from "@/components/settings-agent";
import { AppSettingsPanel } from "@/components/settings-app";
import { WorkspaceShell } from "@/components/workspace";
import { appPaths } from "@/routing/paths";

/** Inside the shell like every other page: the rail stays put, so leaving
 *  settings is one click rather than a bounce through the home redirect. */
export function SettingsRoute() {
  return <WorkspaceShell><SettingsPage /></WorkspaceShell>;
}

export function ProfileSectionRoute() {
  return <><DocumentTitle title="Profile" /><ProfilePanel /></>;
}

export function AgentSectionRoute() {
  return <><DocumentTitle title="Agent settings" /><AgentSettingsPanel /></>;
}

export function AppSectionRoute() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // Deleting everything deletes the account itself, so there is nothing left
  // to come back to — the cache goes with it.
  return <>
    <DocumentTitle title="Settings" />
    <AppSettingsPanel onDeleted={() => { queryClient.clear(); navigate(appPaths.login, { replace: true }); }} />
  </>;
}

/** The profile was its own page before settings had sections. */
export function LegacyProfileRedirect() {
  return <Navigate to={appPaths.settings} replace />;
}
