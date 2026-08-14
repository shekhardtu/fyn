import { DocumentTitle } from "@/components/document-title";
import { ProfilePanel } from "@/components/profile";
import { SignInPanel } from "@/components/sign-in";

export function LoginRoute() {
  return <><DocumentTitle title="Sign in" /><SignInPanel /></>;
}

export function ProfileRoute() {
  return <><DocumentTitle title="Profile" /><ProfilePanel /></>;
}
