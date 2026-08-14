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
      {
        path: "profile",
        lazy: async () => ({ Component: (await import("@/routes/auth-routes")).ProfileRoute }),
      },
      {
        path: "overview",
        lazy: async () => ({ Component: (await import("@/routes/money-routes")).OverviewRoute }),
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
        children: [{ path: ":conversationId" }],
      },
      { path: "*", element: <NotFoundRoute /> },
    ],
  },
];

export const appRouter = createBrowserRouter(appRoutes);
