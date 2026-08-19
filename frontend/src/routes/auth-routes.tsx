import { DocumentTitle } from "@/components/document-title";
import { SignInPanel } from "@/components/sign-in";

export function LoginRoute() {
  return <><DocumentTitle title="Sign in" /><SignInPanel /></>;
}
