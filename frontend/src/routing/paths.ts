export const appPaths = Object.freeze({
  home: "/",
  login: "/login",
  profile: "/profile",
  overview: "/overview",
  transactions: "/transactions",
  categories: "/categories",
  conversation: (conversationId: string) => `/c/${encodeURIComponent(conversationId)}`,
});

export const appRoutePatterns = Object.freeze({
  conversation: "/c/:conversationId",
});
