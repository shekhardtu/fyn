# Next.js to Vite migration plan

**Status:** Ready to start after the current AG-UI cutover is committed and its browser adapter is covered by focused tests

**Last refreshed:** 2026-08-14

**Scope:** `frontend/` only, with deployment/configuration changes needed to serve it as a static SPA

## Executive decision

Migrate the web app from Next.js to a static Vite single-page application using React Router in Data Mode. Keep TanStack Query as the data-loading/cache layer and keep the browser connected directly to the FastAPI AG-UI runtime.

The target does **not** need a Node or Express server:

```text
Vite SPA in the browser
  ├─ REST requests ────────────────┐
  └─ AG-UI SSE/replay/interrupts ──┼──> FastAPI `/api/*`
                                   └──> persisted AG-UI run/event state
```

CopilotKit is no longer part of the repository or the migration. The former plan's CopilotKit gateway, `/api/copilotkit` proxy, compatibility route, and server-side header forwarding are obsolete and must not be recreated.

## Current-state assessment

The repository has already completed the most important architectural cutover:

- `@copilotkit/*` dependencies and the Next API route have been removed.
- The browser uses `@ag-ui/client` and `@ag-ui/core` version `0.0.57` directly.
- `FynHttpAgent` sends credentialed requests to FastAPI and supports capabilities, streaming, run replay, cancellation, custom response events, reasoning/activity events, and human-in-the-loop interrupts.
- FastAPI exposes the durable AG-UI runtime under `/api/agent`, including capabilities, thread state, replay/follow, and cancellation endpoints.
- AG-UI runs, events, and interrupts are persisted, and active runs are recovered at backend startup.
- There are no remaining Next API routes. Next.js is now responsible for browser routing, layouts, metadata, fonts, compilation, and static delivery—not for the agent transport.

This makes the Vite migration substantially smaller than the previous plan. The main risk is preserving UI/router lifecycle behavior and the AG-UI client's reconnect semantics, not replacing a server gateway.

### Validation baseline

The refreshed codebase currently has this baseline:

- Frontend unit tests: 18 files and 289 tests pass.
- Frontend lint: passes with two existing React Compiler warnings around TanStack Virtual.
- Targeted backend AG-UI, frontend-contract, and migration tests: 10 tests pass.
- Next production build with webpack: passes; it emits an optional `vega-canvas`/`canvas` resolution warning.
- The default Turbopack build cannot bind an internal port in the current execution environment. This is an environment limitation, not an observed application compile error.
- `npm ls --depth=0` reports several extraneous WASM/Sharp packages in the existing install. A clean `npm ci` is required before taking the migration baseline.

## Migration invariants

These are requirements, not opportunities for redesign:

1. The browser continues to talk directly to FastAPI with `credentials: "include"`.
2. Keep the current AG-UI request/event contract and pin `@ag-ui/client` and `@ag-ui/core` to `0.0.57` during the framework migration.
3. A dropped AG-UI stream resumes through `GET /api/agent/runs/{runId}/events?after={sequence}` without duplicating already-applied events.
4. Interrupt discovery, approval/rejection, cancellation, reasoning summaries, activity traces, and custom `fyn.response.v1` events retain their current behavior.
5. The conversation workspace remains mounted while navigating between `/c/:conversationId` routes. Composer state, transcript state, and rail scroll must not reset due to a router remount.
6. Authentication bootstrap and redirects must complete before protected content is displayed.
7. Existing URLs remain valid: `/`, `/c/:conversationId`, `/overview`, `/transactions`, `/categories`, `/login`, and `/profile`.
8. Keep port `3000` in local development so existing CORS, Google OAuth origins, Docker configuration, and Playwright assumptions remain stable.

## Target stack

| Concern | Target | Notes |
| --- | --- | --- |
| Build/dev server | Vite 8 | Node 24 LTS for local and CI builds |
| React integration | `@vitejs/plugin-react` 6 | Keep React and React DOM 19.2.x |
| Browser routing | React Router 8, Data Mode | TanStack Query remains the data layer |
| Styling | Tailwind CSS 4 + `@tailwindcss/vite` | Remove the PostCSS-specific Next setup |
| Unit tests | Vitest 4 + Testing Library | Reuse Vite aliases/configuration |
| End-to-end tests | Playwright | Keep the existing suite and base URL |
| Fonts | Self-hosted Fontsource variable packages or local WOFF2 | Replaces `next/font/google` |
| Production delivery | Static `dist/` through Nginx or the existing static host | SPA fallback required |
| Agent transport | AG-UI `0.0.57`, direct to FastAPI | No frontend application server |

Do not combine a React, AG-UI, visualization-library, or design-system upgrade with this migration. TypeScript should remain on 5.9.x because the current `typescript-eslint` line does not support TypeScript 7.

## Route and layout design

Use one browser router. A minimal route tree that mirrors current behavior is:

```text
/
├─ /login
├─ /profile
├─ /overview
├─ /transactions
├─ /categories
├─ /c                         WorkspaceShell layout
│  └─ /c/:conversationId      route marker; shell reads the matched ID
└─ *                          NotFound
```

The `/c` layout is load-bearing. Its element must own `WorkspaceShell`, while the `:conversationId` child changes without replacing that element. Because a parent route should not assume it receives a child route's params, the shell should resolve the active conversation with `useMatch("/c/:conversationId")` or receive it from a stable wrapper.

For the first cutover, retain the existing wrappers for `/overview`, `/transactions`, and `/categories`. Consolidating them under a broader authenticated shell can be a later refactor; it would change remount behavior and needlessly widen this migration.

Navigation mapping:

| Next.js | React Router |
| --- | --- |
| `useRouter().push(path, { scroll: false })` | `navigate(path, { preventScrollReset: true })` |
| `useRouter().replace(path)` | `navigate(path, { replace: true })` |
| `usePathname()` | `useLocation().pathname` |
| `useParams()` | `useParams()` in the matched route, or `useMatch()` from the persistent parent |
| `router.prefetch()` | Remove for conversation URLs; TanStack Query already prefetches conversation data |

Lazy-load the less frequently used money/profile route modules if bundle inspection shows a material benefit. Do not lazy-load the core conversation workspace during the initial parity cutover.

## Metadata and fonts

Move static metadata and viewport defaults from `src/app/layout.tsx` to `index.html`. Keep dynamic conversation titles on the client using the existing title behavior or React 19's native `<title>` support.

This changes the initial response for `/c/:conversationId`: it will contain the default title and update after conversation data loads instead of receiving a server-generated title. That is an acceptable parity exception for this authenticated application unless server-rendered social/SEO metadata becomes a product requirement.

Replace `next/font/google` with self-hosted assets. Preserve the current CSS variable names so the rest of the style system does not need to change.

## Environment and API configuration

Create one typed environment module, for example `src/config/env.ts`, and stop reading environment variables throughout feature code.

| Current | Vite |
| --- | --- |
| `NEXT_PUBLIC_API_URL` | `VITE_API_URL` |
| `NEXT_PUBLIC_GOOGLE_CLIENT_ID` | `VITE_GOOGLE_CLIENT_ID` |

Browser code reads values through `import.meta.env`. Node-based Playwright/config code may read `process.env.VITE_API_URL` where needed. Document that `VITE_*` values are public and embedded at build time.

Production should keep the app and API on same-site sibling origins, or another cookie-compatible topology already allowed by the backend. FastAPI CORS must continue allowing the exact frontend origin, credentials, and the `Last-Event-ID` header. A same-origin `/api` reverse proxy can be introduced later, but it is not required for this migration.

## File-level change map

### Add

- `frontend/index.html`
- `frontend/vite.config.ts`
- `frontend/src/main.tsx`
- `frontend/src/router.tsx`
- `frontend/src/config/env.ts`
- `frontend/src/routes/not-found.tsx`
- Focused AG-UI adapter tests, preferably `frontend/src/lib/api.agent.test.ts`
- Static-server configuration such as `frontend/nginx.conf`

### Move or adapt

- `src/app/globals.css` to a Vite-owned global stylesheet import.
- App providers, focus management, and skip-link markup from `src/app/layout.tsx` into a root React component.
- Page components under `src/app/**/page.tsx` into framework-neutral route modules.
- `WorkspaceShell` navigation from `next/navigation` to React Router.
- `src/lib/api.ts` environment access to the typed Vite environment module.
- Tests that mock `next/navigation` or `next/headers` to router-backed tests using `createMemoryRouter` and `RouterProvider`.
- Docker Compose/build arguments from `NEXT_PUBLIC_*` to `VITE_*`.

### Remove after parity passes

- `next`, `eslint-config-next`, and `@tailwindcss/postcss` dependencies.
- `next.config.*`, `next-env.d.ts`, and the Next app-router wrappers.
- Next-specific metadata tests and navigation mocks.
- The Next production runtime/container layer.

Do not remove the Next entry points during the scaffold phase. Keeping dual scripts briefly makes comparison and rollback inexpensive.

## Implementation phases

### Phase 0 — Freeze the AG-UI baseline

The current AG-UI cutover is a large uncommitted change. Land or otherwise freeze it before mixing in build-system changes so protocol regressions can be distinguished from migration regressions.

1. Run a clean `npm ci` and record the baseline again.
2. Add focused browser-adapter tests for:
   - credentialed POST request shape and `fyn.response.v1` handling;
   - disconnect followed by replay GET with the last sequence cursor;
   - replay event deduplication;
   - interrupt mapping and resume payload;
   - cancel behavior;
   - capability rejection/fallback behavior;
   - per-thread agent isolation and conversation hydration.
3. Keep the backend AG-UI protocol tests and existing conversation E2E test green.
4. Optionally rename the internal `CopilotWorkspace` component to `ConversationWorkspace`. This is terminology cleanup only; no CopilotKit runtime remains.

**Exit gate:** the direct AG-UI transport has deterministic tests independent of Next.js.

### Phase 1 — Add a parallel Vite entry point

1. Install Vite, the React plugin, React Router, and `@tailwindcss/vite`.
2. Add `index.html`, Vite config, root entry, providers, global CSS, aliases, and typed environment configuration.
3. Configure development on port `3000` with `strictPort: true`.
4. Self-host the two current font families and preserve their CSS variables.
5. Add a `typecheck` script.
6. Temporarily expose both toolchains:
   - `dev:vite`, `build:vite`, `preview:vite`;
   - `dev:next`, `build:next` until parity is accepted.

**Exit gate:** the Vite root renders with providers/styles, unit tests and typecheck pass, and no product route has been removed.

### Phase 2 — Move routing without changing product behavior

1. Define the route tree above and migrate navigation hooks.
2. Preserve the stable `/c` shell across conversation parameter changes.
3. Recreate auth bootstrap/redirect behavior.
4. Move page metadata and title behavior to the client/static document.
5. Replace Next router mocks with memory-router tests.
6. Remove redundant route prefetching while retaining TanStack Query conversation prefetch.

Add an explicit lifecycle test that types a composer draft, changes conversation routes, and proves the shell node was not remounted unexpectedly. Also verify transcript/rail scroll behavior and encoded conversation IDs.

**Exit gate:** all routes, redirects, back/forward navigation, deep links, and persistent-shell behavior match the Next app.

### Phase 3 — Static production delivery

1. Produce `dist/` with Vite.
2. Replace the Next runtime image with a static Nginx image, or configure the chosen static host.
3. Configure SPA fallback to `index.html` for application routes.
4. Serve hashed `/assets/*` with immutable caching and `index.html` with no-cache/revalidation headers.
5. Ensure missing asset requests return 404 rather than falling back to HTML.
6. Pass `VITE_API_URL` and `VITE_GOOGLE_CLIENT_ID` as build-time values.
7. Verify credentialed API and AG-UI requests from the real production-like origin.

**Exit gate:** direct loads and refreshes of every route work in the production container, and authentication cookies plus AG-UI SSE/replay work cross-origin.

### Phase 4 — Full parity and cutover

Run the complete frontend and backend suites, then add or confirm E2E coverage for:

- sign-in/bootstrap and protected redirects;
- create, open, rename, and delete conversation flows;
- streaming text and custom response rendering;
- visible activity/reasoning updates;
- disconnect/reconnect with replay and no duplicate UI events;
- human-in-the-loop approval/rejection and refresh recovery;
- run cancellation;
- overview, transactions, categories, and profile routes;
- browser back/forward, deep links, refreshes, and not-found behavior.

Inspect the production bundle. Pay particular attention to Vega's optional canvas dependency and ensure no Node-only module is pulled into the browser chunk.

**Exit gate:** Vite meets all acceptance criteria in CI and a production-like container.

### Phase 5 — Remove Next.js

1. Make Vite the default `dev`, `build`, and `start`/`preview` workflow.
2. Remove Next dependencies, configuration, app-router wrappers, and dual-build scripts.
3. Update README, Docker, CI, and contributor documentation.
4. Run `npm ci`, lint, typecheck, unit tests, Vite build, Playwright, and backend tests one final time.

**Exit gate:** a clean install contains no Next or CopilotKit package, and production runs solely from the static Vite artifact.

## Acceptance criteria

The migration is complete only when all of the following are true:

- `npm ci` is clean and `npm ls --depth=0` has no unexplained extraneous packages.
- Lint, typecheck, frontend unit tests, backend tests, and Playwright pass.
- `npm run build` produces a static `dist/` without relying on a Node application server.
- Every supported route survives direct load, refresh, back/forward navigation, and SPA fallback.
- The conversation shell does not remount merely because `conversationId` changes.
- All API and AG-UI calls use the configured FastAPI origin with credentials.
- AG-UI streaming, cursor replay, deduplication, cancellation, capabilities, interrupts, activity, reasoning, and custom response events retain parity.
- No `next/*`, `@copilotkit/*`, or old frontend proxy endpoint is present in shipped code.
- Production cache headers do not pin a stale `index.html`, and missing assets do not return the SPA document.

## Primary risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Conversation shell remounts on param changes | Stable `/c` parent element plus a lifecycle test that checks the actual shell node/state |
| SSE replay duplicates UI state | Pin AG-UI versions, preserve sequence cursors, and add adapter-level replay/deduplication tests before routing work |
| Credentialed cross-origin requests fail | Keep port/origin stable locally; test exact production origins, CORS credentials, cookies, and `Last-Event-ID` |
| SPA fallback masks missing JS/CSS assets | Apply fallback only to app navigation and return real 404s for asset paths |
| Build-time env points at the wrong API | Central typed config, CI validation, and an environment banner/health check in staging |
| Optional Vega canvas code affects Vite build | Inspect dependency resolution and final chunks; avoid installing server-only canvas unless a browser feature requires it |
| Protocol and framework changes become inseparable | Commit/freeze the AG-UI cutover and its focused tests before Vite implementation |
| Dynamic title loses server-rendered value | Accept default-then-client title behavior explicitly; revisit SSR only if it becomes a real product requirement |

## Suggested pull-request sequence

1. **AG-UI baseline:** land the current protocol/runtime cutover and add focused web adapter tests.
2. **Dual-build scaffold:** add Vite, environment config, fonts, Tailwind integration, and parallel scripts without removing Next.
3. **Router cutover:** migrate routes/navigation/metadata and prove persistent-shell parity.
4. **Production cutover:** static container, cache/fallback rules, full E2E, bundle review, then remove Next.

Each PR should remain deployable or trivially revertible. Do not upgrade the AG-UI packages inside these PRs.

## Effort estimate

Assuming the AG-UI cutover is frozen first, the Vite work is approximately **4–6 engineering days**:

- Baseline and adapter tests: 0.5–1 day
- Vite/env/fonts/Tailwind scaffold: 0.5–1 day
- Router and test migration: 1–1.5 days
- Static deployment and full parity testing: 1.5–2.5 days
- Next cleanup and documentation: about 0.5 day

The estimate excludes fixing unrelated product bugs uncovered by the broader E2E run.

## Decisions that do not block starting

Use these defaults unless deployment requirements say otherwise:

- Serve the current Docker deployment through Nginx and keep the browser talking directly to the configured FastAPI origin.
- Accept client-updated conversation titles instead of preserving server-rendered metadata.
- Keep the existing route/component structure during cutover; consolidate layouts only after parity.
- Keep AG-UI packages pinned to `0.0.57` until the migration is complete.

The first implementation task is therefore Phase 0: commit/freeze the current AG-UI cutover and add the focused `FynHttpAgent` tests. There is no remaining CopilotKit or gateway decision blocking the migration.

## Reference material

- Local runtime contract: [`AG_UI_RUNTIME.md`](./AG_UI_RUNTIME.md)
- Vite 8 announcement and requirements: <https://vite.dev/blog/announcing-vite8>
- Vite static deployment guide: <https://vite.dev/guide/static-deploy.html>
- React Router modes: <https://reactrouter.com/start/modes>
- React Router changelog: <https://reactrouter.com/home/changelog>
- Tailwind CSS Vite integration: <https://tailwindcss.com/docs/installation/using-vite>
- React 19 document metadata: <https://react.dev/blog/2024/12/05/react-19>
