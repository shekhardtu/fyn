import { createBrowserRouter, type RouteObject } from "react-router";
import { AppRoot } from "@/app/app-root";
import { InitialRouteFallback, NotFoundRoute, RouteErrorBoundary } from "@/routes/fallback-routes";

/** A single, inspectable route table keeps URL ownership out of feature UI. */
export const appRoutes: RouteObject[] = [
  {
    path: "/",
    element: <AppRoot />,
    errorElement: <RouteErrorBoundary />,
    hydrateFallbackElement: <InitialRouteFallback />,
    children: [
      {
        index: true,
        lazy: async () => ({ Component: (await import("@/routes/workspace-routes")).HomeRoute }),
      },
      {
        path: "login",
        lazy: async () => ({ Component: (await import("@/routes/auth-routes")).LoginRoute }),
      },
      // Settings is one page with three sections, so each section owns a URL:
      // a link into agent settings has to survive being sent to someone else.
      {
        path: "settings",
        lazy: async () => ({ Component: (await import("@/routes/settings-routes")).SettingsRoute }),
        children: [
          { index: true, lazy: async () => ({ Component: (await import("@/routes/settings-routes")).ProfileSectionRoute }) },
          { path: "agent", lazy: async () => ({ Component: (await import("@/routes/settings-routes")).AgentSectionRoute }) },
          { path: "app", lazy: async () => ({ Component: (await import("@/routes/settings-routes")).AppSectionRoute }) },
        ],
      },
      // The profile used to be its own page; links to it still exist.
      {
        path: "profile",
        lazy: async () => ({ Component: (await import("@/routes/settings-routes")).LegacyProfileRedirect }),
      },
      {
        path: "overview",
        lazy: async () => ({ Component: (await import("@/routes/money-routes")).OverviewRoute }),
      },
      {
        path: "dashboards",
        lazy: async () => ({ Component: (await import("@/routes/money-routes")).DashboardsRoute }),
      },
      {
        path: "transactions",
        lazy: async () => ({ Component: (await import("@/routes/money-routes")).TransactionsRoute }),
      },
      {
        path: "categories",
        lazy: async () => ({ Component: (await import("@/routes/money-routes")).CategoriesRoute }),
      },
      {
        path: "c",
        lazy: async () => ({ Component: (await import("@/routes/workspace-routes")).ConversationLayoutRoute }),
        // The layout owns the thread so it stays mounted while only the id
        // changes. Give the leaf an explicit empty element: omitting it makes
        // React Router warn that this valid URL may render a blank page.
        children: [{ path: ":conversationId", element: <></> }],
      },
      { path: "*", element: <NotFoundRoute /> },
    ],
  },
];

export const appRouter = createBrowserRouter(appRoutes);
