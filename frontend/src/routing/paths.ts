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
  loans: "/loans",
  loan: (loanId: string) => `/loans/${encodeURIComponent(loanId)}`,
  loanInvitation: (token: string) => `/loan-invitations/${encodeURIComponent(token)}`,
  conversation: (conversationId: string) => `/c/${encodeURIComponent(conversationId)}`,
});

export const appRoutePatterns = Object.freeze({
  conversation: "/c/:conversationId",
  loan: "/loans/:loanId",
  loanInvitation: "/loan-invitations/:token",
});
