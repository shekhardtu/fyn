# Frontend architecture

The web client is a static Vite SPA. React Router owns URL state, TanStack Query owns remote state, and the browser talks directly to FastAPI over credentialed REST and AG-UI.

## Runtime flow

```text
main.tsx
  → app/router.tsx
    → routes/* (URL adapters)
      → components/* (product UI)
        → lib/api.ts (REST + AG-UI)
          → FastAPI
```

There is no browser-facing Node server, server component layer, or frontend API proxy.

## Directory map

```text
src/
├── app/          application composition, route table, global CSS, favicon
├── config/       typed public environment boundary
├── routes/       thin route-level composition grouped by product area
├── routing/      route patterns and encoded path builders
├── components/   product UI and colocated component tests
│   ├── ui/               reusable visual/accessibility primitives
│   └── widget-library/   typed financial widget implementations
├── lib/           API transport, protocol readers, formatting, state logic
│   └── generated/        generated contracts; never edit directly
├── test/          shared test setup only
└── main.tsx       browser entry point
```

This structure is intentionally shallow. A folder exists only when it communicates ownership. Avoid catch-all `shared`, `common`, or `helpers` directories; name a module after the concept it owns.

## Dependency direction

- `main.tsx` may import `app/` and global assets.
- `app/` may import `routes/`, providers, and router infrastructure.
- `routes/` may compose `components/` but should contain little or no business logic.
- `components/` may use `lib/`, `config/`, and `routing/`.
- `lib/` must not import React route components or application composition.
- `config/` and `routing/` are leaf infrastructure modules and should remain small.

Direct imports are preferred over barrel files because they make dependency ownership visible and give code-search and LLM tools precise context.

## State ownership

- URL state: React Router.
- Remote/server state: TanStack Query.
- Form, overlay, draft, and transient interaction state: local React state in the owning feature.
- Durable financial and agent-run state: FastAPI/PostgreSQL, never browser storage.
- AG-UI stream projection: `lib/api.ts`, consumed through callbacks by the conversation workspace. The first answer fragment is delivered immediately; subsequent fragments coalesce to the latest value once per animation frame, and the terminal fragment is synchronously flushed before the run resolves.

Do not add another global store unless a concrete state domain cannot fit one of these owners.

## Routing

`app/router.tsx` is the complete route inventory. Route modules are lazy-loaded by product group. All navigation targets come from `routing/paths.ts`, which also guarantees conversation IDs are encoded consistently.

The `/c` route is nested deliberately:

```text
/c                    ConversationLayoutRoute → WorkspaceShell
└── :conversationId   matched marker; WorkspaceShell selects the thread
```

The parent element survives conversation-ID changes. Preserve the lifecycle test in `app/router.test.tsx` whenever routing changes.

## API and AG-UI boundary

`lib/api.ts` is the browser transport facade. It validates server payloads against generated Zod contracts and owns the `FynHttpAgent` instances. Important protocol behavior includes:

- credentials on every API and AG-UI request;
- one AG-UI agent per conversation thread;
- persisted run IDs and monotonic replay cursors;
- replay-event deduplication;
- non-blocking capability prefetch for ordinary streaming and a hard check
  before interrupt resume;
- typed custom response, activity, reasoning, and interrupt projection;
- cooperative run cancellation.

Protocol details and backend endpoints are documented in [`../docs/AG_UI_RUNTIME.md`](../docs/AG_UI_RUNTIME.md).

## When a feature grows

Keep a small feature in `components/` with its test. When an area develops several private components plus its own hooks or query definitions, move that area as a unit to `src/features/<feature>/`; keep its public route adapter in `routes/`. Do not pre-create `components`, `hooks`, `services`, or `types` subfolders until each has real content.

## Production

`yarn build` emits `dist/`. The production image serves it through unprivileged Nginx with immutable hashed assets, non-cached `index.html`, real asset 404s, and SPA fallback for application routes. `VITE_API_URL` and `VITE_GOOGLE_CLIENT_ID` are public values embedded during the build.
