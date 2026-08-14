import { Link, isRouteErrorResponse, useRouteError } from "react-router";
import { DocumentTitle } from "@/components/document-title";
import { appPaths } from "@/routing/paths";

/** Visible only while the first lazy route module is loading. */
export function InitialRouteFallback() {
  return <main className="grid min-h-dvh place-items-center bg-ground p-6">
    <p role="status" className="text-control text-ink-muted">Opening fyn AI…</p>
  </main>;
}

export function NotFoundRoute() {
  return <main className="grid min-h-dvh place-items-center bg-ground p-6 text-center">
    <DocumentTitle title="Page not found" />
    <div>
      <p className="text-note font-semibold tracking-[0.12em] text-ink-muted uppercase">404</p>
      <h1 className="mt-2 font-heading text-title font-semibold text-ink">This page does not exist</h1>
      <p className="mt-2 text-control text-ink-muted">The address may be old, or the page may have moved.</p>
      <Link to={appPaths.home} className="mt-5 inline-flex h-10 items-center rounded-lg bg-secondary px-4 text-control font-semibold text-on-secondary hover:bg-secondary-hover">
        Return to fyn AI
      </Link>
    </div>
  </main>;
}

export function RouteErrorBoundary() {
  const error = useRouteError();
  const missing = isRouteErrorResponse(error) && error.status === 404;
  if (missing) return <NotFoundRoute />;

  return <main className="grid min-h-dvh place-items-center bg-ground p-6 text-center">
    <DocumentTitle title="Something went wrong" />
    <div role="alert">
      <h1 className="font-heading text-title font-semibold text-ink">We couldn’t open this page</h1>
      <p className="mt-2 text-control text-ink-muted">Reload the page. Your financial data has not been changed.</p>
      <Link to={appPaths.home} className="mt-5 inline-flex h-10 items-center rounded-lg bg-secondary px-4 text-control font-semibold text-on-secondary hover:bg-secondary-hover">
        Return to fyn AI
      </Link>
    </div>
  </main>;
}
