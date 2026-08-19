export const appPaths = Object.freeze({
  home: "/",
  login: "/login",
  settings: "/settings",
  settingsAgent: "/settings/agent",
  settingsApp: "/settings/app",
  overview: "/overview",
  dashboards: "/dashboards",
  transactions: "/transactions",
  categories: "/categories",
  conversation: (conversationId: string) => `/c/${encodeURIComponent(conversationId)}`,
});

export const appRoutePatterns = Object.freeze({
  conversation: "/c/:conversationId",
});
