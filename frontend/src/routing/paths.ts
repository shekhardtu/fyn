export const appPaths = Object.freeze({
  home: "/",
  login: "/login",
  settings: "/settings",
  settingsAgent: "/settings/agent",
  settingsApp: "/settings/app",
  overview: "/overview",
  dashboards: "/dashboards",
  transactions: "/transactions",
  newTransaction: "/transactions?new=1",
  categories: "/categories",
  loans: "/loans",
  loan: (loanId: string) => `/loans/${encodeURIComponent(loanId)}`,
  loanInvitation: (token: string) => `/loan-invitations/${encodeURIComponent(token)}`,
  conversation: (conversationId: string, prompt?: string) => `/c/${encodeURIComponent(conversationId)}${prompt?.trim() ? `?${new URLSearchParams({ prompt: prompt.trim() })}` : ""}`,
});

export const appRoutePatterns = Object.freeze({
  conversation: "/c/:conversationId",
  loan: "/loans/:loanId",
  loanInvitation: "/loan-invitations/:token",
});

export type FinanceFormKind = "budget" | "goal" | "account" | "contribution";

/** Form URL state preserves the selected month and survives reload/back. */
export function financeFormParams(previous: URLSearchParams, form: FinanceFormKind | null, id?: string, scope?: "category") {
  const next = new URLSearchParams(previous);
  for (const key of ["form", "record", "scope"]) next.delete(key);
  if (form) next.set("form", form);
  if (id) next.set("record", id);
  if (scope) next.set("scope", scope);
  return next;
}
