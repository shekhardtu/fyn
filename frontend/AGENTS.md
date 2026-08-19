# Frontend agent guide

Read [`ARCHITECTURE.md`](./ARCHITECTURE.md) before changing application structure and [`../docs/AG_UI_RUNTIME.md`](../docs/AG_UI_RUNTIME.md) before changing agent transport code.

## Commands

- `yarn dev` — Vite on port 3000
- `yarn typecheck` — TypeScript without emitting files
- `yarn lint` — ESLint
- `yarn test` — Vitest
- `yarn build` — typecheck plus the production Vite build
- `yarn test:e2e` — Playwright against the configured frontend and API

Run typecheck, lint, tests, and build for structural changes.

## Placement rules

- `src/app/` composes providers and the route table. Do not put product logic here.
- `src/routes/` contains thin URL-level adapters. Group related routes; do not duplicate feature components in this directory.
- `src/components/` contains product UI. Reusable primitives belong in `components/ui/`; widget implementations belong in `components/widget-library/`.
- `src/lib/` contains framework-neutral API, protocol, formatting, and state utilities.
- `src/config/` is the only place that reads `import.meta.env`.
- `src/routing/` owns path patterns and URL builders. Do not hand-build application URLs in components.

Prefer a shallow feature module over `utils`, `helpers`, `common`, barrel files, or single-use abstraction layers. Split a product area into `src/features/<name>/` only when it has enough components, hooks, and tests that the current location is no longer easy to scan.

## Invariants

- The browser talks directly to FastAPI using credentialed REST and AG-UI requests. Do not add a frontend server or transport proxy.
- Keep `@ag-ui/client` and `@ag-ui/core` pinned together. Replay cursors, event deduplication, interrupts, cancellation, and `fyn.response.v1` are compatibility boundaries.
- The `/c` parent route owns `WorkspaceShell`; changing `conversationId` must not remount that shell.
- TanStack Query owns server-state caching. Do not copy API data into a second global store.
- Generated protocol files are generated artifacts; change their backend source/generator instead of editing them by hand.
- `VITE_*` variables are public build-time values. Never place a secret in frontend configuration.
- New behavior needs a focused test near the module that owns it. Router behavior should use a memory router rather than mocking navigation hooks.
