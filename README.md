# fyn AI

An AI-native personal finance application whose primary interface is a conversation. Natural language is interpreted into structured drafts; deterministic domain services decide what may be committed; PostgreSQL stores canonical truth; and ambiguous changes wait for the user.

## What works

- Passwordless accounts: sign in with a phone number, an email address, or Google. Codes are six digits, live ten minutes, allow five attempts, and are rate-limited per destination.
- One account, both methods: link a phone number to a Google account, or an email address to a phone account, and either signs you in. A later Google sign-in whose address is already verified is adopted rather than duplicated.
- A phone number or email address belongs to exactly one account, enforced by a unique constraint. Linking one that is spoken for is refused before a code is sent; deleting the account holding it releases it.
- Chat-first responsive UI with persistent conversations and one primary composer.
- State-aware orchestration: greetings and bare amounts use safe fast paths; every semantic financial request is routed by Agno, compiled to a typed domain action, independently validated by a fast model, and rerouted through a stronger model when rejected.
- Governed analysis-tool factory: Agno can propose new declarative capabilities; the harness validates, compiles, executes, verifies, versions, repairs, saves, and reuses them without accepting arbitrary code or SQL.
- Live in-chat agent activity showing the selected tool, individual stage timing, cumulative timing, model, and total runtime; traces persist after refresh.
- Native AG-UI transport with ordered lifecycle, text, activity, safe reasoning, tool-call, state snapshot/delta, interrupt, error, and completion events. Runs survive navigation or a dropped client connection and can be replayed from the persisted event log.
- Typed, Zod/Pydantic-validated widget protocol; the model never emits arbitrary UI code.
- Durable widget action receipts: completed or cancelled HITL controls persist their submitted values, become read-only immediately, and remain read-only after refresh.
- Stateful transaction drafts with category/subcategory clarification, user-scoped taxonomy creation, edit, confirmation, and idempotent commit. The router receives the active workflow state and may choose only state-valid actions.
- Expense, income, refund, investment, loan-payment, and transfer intent classification.
- Conversational account resolution for transfers; accounts are created only after confirmation.
- Monthly budgets and savings goals with staged creation/contributions and live progress widgets.
- User-owned merchant category learning; an explicit correction overrides later AI inference.
- Indian amount parsing (`₹2,000`, `3 lakh`, `20k`) and relative dates.
- Grounded spending summaries, category breakdowns, month comparisons, change drivers, largest expenses, recurring-pattern detection, and saved analyses.
- Versioned finance semantic registry covering governed entities, physical fields, approved relationships, metrics, dimensions, field types, time semantics, and query-cost policy. Generated plans never contain SQL.
- Generic semantic query plans over transactions, accounts, budgets, goals, loans, recurring patterns, and subscriptions, with deterministic comparison/ranking/share/period-change/change-driver transforms.
- Recommendation context from saved budgets, goals, loans, accounts, and recurring expenses; the harness rejects recommendations without relevant user-specific context.
- Deterministic affordability, loan/prepayment, and investment-projection calculators using minor units/decimal math.
- Financial observations separated from canonical transactions, with multi-source provenance.
- Structured transaction location labels, tags, spend nature, and append-only per-field provenance/user-correction records.
- Idempotent SMS, email, bank, API, and CSV ingestion.
- Configurable reconciliation scoring, deterministic exact/strong matches, ambiguity review, and explicit merge/keep-separate actions.
- Message classification that rejects OTPs, promotions, balance-only messages, and order confirmations without proof of payment.
- PostgreSQL schema, composite reconciliation indexes, UUID foreign keys, and Alembic migration.
- Narrow Agno evaluator for ambiguous reconciliation; it returns typed advice and cannot merge data.
- Confirmation-safe CSV statement upload: rows are staged, summarized, and reconciled only after Import.
- Privacy settings for location opt-in, source revocation, complete JSON export, and explicit data deletion.

## Architecture

```text
Vite HttpAgent SPA / Expo native AG-UI adapter + typed widgets
                 │  AG-UI over SSE
                 ▼
FastAPI AG-UI runtime + durable runs/events/interrupts
                 │
                 ▼
Conversation harness + persisted workflow state
        │
        ├── bare amount ──────────────────► safe deterministic clarification
        ├── explicit rich analysis ───────► governed Luna router
        └── unified Luna read agent
                 │ authenticated read/calculation tools
                 ├── conversation/read ──► contextual answer from the same run
                 ├── complete typed read ─► governed query executor
                 └── generic handoff ─────► governed Luna router
                                                   │
                                      fast Luna contract validator
                                                   │
                                  rejected ────────┴──────► Terra reroute
                                                   │
                         typed command/query or declarative AnalysisTool spec
                                                   │
                         versioned semantic registry
                      entity + field + relationship context
                                                   │
                           validate → compile → execute → verify → persist
                                          │
                  ┌───────────────────────┴──────────────────────┐
                  ▼                                              ▼
       Transaction/reconciliation commands              Read-only semantic queries
                  │                                      + deterministic calculators
                  └───────────────────────┬──────────────────────┘
                                          ▼
                                      PostgreSQL
```

Financial facts, drafts, conversations, preferences, and provenance are stored separately. The LLM is never the database and important calculations are not delegated to it.

The web client uses AG-UI's official `HttpAgent`. AG-UI does not currently ship a React Native adapter, so Expo uses its native streaming fetch with the official `@ag-ui/core` event schemas as a deliberately thin transport adapter. Both clients consume the same protocol and Fyn projection. The typed widget protocol, governed finance actions, and BI rendering remain application code, so AG-UI does not hand financial authority to client-provided tools or arbitrary generated UI. See [the AG-UI runtime contract](docs/AG_UI_RUNTIME.md) for event, reconnect, interrupt, cancellation, and capability semantics.

The semantic registry is reconstructed from versioned application code for every planner/validator contract; it is not remembered in model chat history. Startup drift checks verify unique semantic names, every exposed field and relationship against SQLAlchemy, and complete compiler adapters for every queryable dimension and join. Relationships needed only for domain context are explicitly marked non-queryable. Before execution, the deterministic compiler independently verifies the metric/base-entity pairing, dimension and filter availability, approved joins, operator/type compatibility, integer-minor-unit money values, event-versus-snapshot time behavior, tenant scope, and bounded query cost. Only parameterized SQLAlchemy `SELECT` statements can be produced. Each result includes the registry version and schema hash for lineage.

Generated analysis tools are reproducible cache entries rather than financial truth. A registry version/hash change deletes incompatible tool specifications and execution runs; generating a revised plan for the same normalized intent replaces its prior cached variant. Canonical financial and conversation records are not affected by this cleanup lifecycle.

## Run locally

The running application uses PostgreSQL and Alembic exclusively owns its schema. SQLite is used only inside isolated unit tests.

Create the backend environment and put your OpenAI key there—the key must never be added to the frontend:

```bash
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env.local
# Edit backend/.env and set OPENAI_API_KEY=...
# For Google web sign-in, copy GOOGLE_CLIENT_ID into
# frontend/.env.local as VITE_GOOGLE_CLIENT_ID.
```

Sign-in works with no provider accounts: leave the MSG91, Postmark, and Google
values empty and codes are printed to the API log and returned by the API
(`OTP_DEBUG_ECHO=true`). Deployments set `ENVIRONMENT=production`, which turns
every one of those shortcuts into a startup failure.

Start PostgreSQL, migrate the schema, and run the API:

```bash
python3 -m venv .venv
.venv/bin/pip install -e './backend[dev]'
docker compose up -d postgres
cd backend
../.venv/bin/alembic upgrade head
../.venv/bin/uvicorn app.main:app --reload
```

In another terminal:

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:3000`.

Alternatively, run the complete container stack after creating `backend/.env`:

```bash
docker compose --env-file backend/.env up --build
```

The explicit Compose environment file is important: it makes the public
`GOOGLE_CLIENT_ID` available as the frontend's Vite build argument while the
backend continues to read the same file as its service environment. Without
it, Compose would build a frontend with Google sign-in hidden unless that value
also happened to exist in the invoking shell.

Check the active mode at `http://localhost:8000/api/health`. It should report `"database":"postgresql"` and `"agent_mode":"llm"`.

## Validate

```bash
cd backend && ../.venv/bin/pytest
cd frontend && npm test && npm run lint && npm run build
cd frontend && npm run test:e2e
cd mobile && npm run typecheck
```

The end-to-end tests exercise bare-amount clarification through save/refresh, rich merchant entry through grounded analytics, confirmation-safe CSV upload, privacy/export controls, and active-conversation persistence. The reconciliation benchmark covers exact replay, cross-source corroboration, pending/posted events, same-amount and same-merchant false-merge traps, refunds, transfers, recurring charges, and human review. Its current gate requires 100% precision, at least 95% recall, zero false merges, and at most 5% false splits; the bundled dataset currently scores 100% precision/recall with zero false merges/splits.

## Accounts and sign-in

Every route below `/api` except `/api/health` and `/api/auth/*` requires a session. The session is an opaque token in a `Secure`, `HttpOnly`, `SameSite=Lax` cookie; the database stores only its SHA-256 digest, so a copy of the table cannot be replayed. Because the cookie is withheld from cross-site writes, the mutating routes need no separate CSRF token — which assumes the app and the API are same-site, as they are when served from sibling subdomains of one host.

One-time codes are stored as an HMAC keyed by `AUTH_SECRET` and bound to their challenge id. Sending a new code retires the previous one for that destination, so pressing resend never widens the guessable space.

An identifier belongs to one account:

- Signing in with an unknown phone number or address creates an account.
- Linking one that another account holds is refused with `409` before any message is sent — the refusal names the remedy, which is to delete that account.
- Deleting an account (`DELETE /api/privacy/data`) releases its phone number and email address.
- The last remaining sign-in method cannot be removed.
- An address that came from Google is managed at Google and is not replaceable by a code here.

The account that predates authentication is adopted by the first sign-in, so local data recorded before this existed stays reachable. Set `CLAIM_SEEDED_USER_ON_FIRST_LOGIN=false` to turn that off.

### Providers

Phone codes go through the MSG91 Flow API and email codes through Postmark; `MSG91_OTP_VARIABLE` must exactly match the case-sensitive variable in your DLT-registered template (`OTP` for `##OTP##`). Google sign-in needs an OAuth 2.0 **Web application** client id from Google Cloud Console → APIs & Services → Credentials, with your origins listed as authorised JavaScript origins. The same value goes to the server as `GOOGLE_CLIENT_ID` and to the browser as `VITE_GOOGLE_CLIENT_ID`; leaving it empty hides Google sign-in and leaves the code flows working.

With no provider credentials the codes are printed to the API log and, with `OTP_DEBUG_ECHO=true`, returned in the response — which is how a fresh checkout and the browser suite sign in. Startup prints exactly which shortcuts are active. Set `ENVIRONMENT=production` and each one becomes a startup failure instead.

## API surfaces

- `GET /api/auth/session` — who the caller is; answers `200` signed in or not.
- `POST /api/auth/otp/start` / `POST /api/auth/otp/verify` — send and present a sign-in code.
- `POST /api/auth/google` — exchange a verified Google credential for a session.
- `POST /api/auth/signout` — end this browser's session, leaving other devices alone.
- `GET /api/profile` — the account and its linked sign-in methods.
- `POST /api/profile/identities/otp/start` / `.../verify` — link or replace a phone number or email address.
- `DELETE /api/profile/identities/{id}` — unlink a sign-in method, never the last one.
- `GET /api/conversations` — one keyset-paged page of history for the rail (`cursor`, `limit`).
- `DELETE /api/conversations/{id}` — erase a thread and every row that points at it; transactions it recorded are kept.
- `GET /api/agent/capabilities` — the implemented AG-UI capability declaration, including Fyn's authority boundaries.
- `POST /api/agent` — start or resume an AG-UI run and stream canonical events over SSE.
- `GET /api/agent/threads/{id}` — active run and open-interrupt projection used to recover UI state.
- `GET /api/agent/runs/{id}/events` — replay and follow a persisted run; accepts `after` or `Last-Event-ID`.
- `POST /api/agent/runs/{id}/cancel` — request cooperative cancellation at a safe execution boundary.
- `POST /api/observations` — ingest an already-structured observation.
- `POST /api/ingest/message` — classify and ingest SMS/email text.
- `POST /api/imports/csv` — idempotent, staged bank CSV review (10 MB limit).
- `GET /api/transactions` — canonical transactions with source counts.
- `GET /api/reconciliation/reviews` — unresolved candidate matches.
- `POST /api/calculators/{affordability,loan,investment}` — deterministic planning tools.
- `GET /api/privacy` — permission and source status.
- `PATCH /api/privacy/location` — explicit location enrichment preference.
- `POST /api/privacy/sources/{source_type}/revoke` — enforce source revocation.
- `GET /api/privacy/export` / `DELETE /api/privacy/data` — export or explicitly delete all user-owned state.

Interactive API documentation is available at `http://localhost:8000/docs`.

## Privacy and safety defaults

- No financial message body is written to application logs or audit metadata.
- Location enrichment is disabled by default and no location is fabricated.
- External IDs, message IDs, source hashes, and import hashes enforce replay protection.
- Financial changes require confirmation; ambiguous reconciliation never silently merges.
- Source field values are retained as provenance when a canonical field is resolved.
- Upload size/type are bounded; direct database access is not exposed to an agent.
- Secrets are read from environment variables and excluded from source control.

- Sessions are opaque and stored hashed; one-time codes are stored as a keyed HMAC and are never logged by a configured provider.
- Session digests and code hashes are deleted with the account but withheld from the data export: they protect the account rather than record anything its owner did.

Before a public deployment, set `ENVIRONMENT=production` (which refuses the development sign-in shortcuts), use a managed secret store, enable database encryption/backups, add a background job runner for large imports/OCR, and complete jurisdiction-specific retention/export/deletion policy review.

## Agent credentials

`OPENAI_API_KEY` in `backend/.env` enables the Agno model path. With `UNIFIED_READ_AGENT_ENABLED=true`, one Luna run owns ordinary conversation and authenticated read/calculation answers from understanding through final wording. It has no mutation authority: a stop-after-call handoff sends transaction changes, taxonomy, budgets/goals, and unsupported finance work into the typed governed pipeline. A handoff that already contains a complete low-risk read query executes directly without a duplicate router; explicit charts and richer analysis enter the semantic planner directly instead of paying for an inevitable handoff first. Deliberately ambiguous bare amounts retain their safe local clarification path. Luna also handles governed routing, extraction, reconciliation advice, and independent response-contract validation; Terra handles complex analysis generation and rejected-route repair. Deterministic services enforce user scope, mutations, read-only queries, calculations, evidence lineage, and state transitions. Governed read/analysis executors produce the final verified copy with their widgets and citations, so no second prose model can add latency or unsupported figures. The in-chat trace reports direct read/handoff selection, retrieval, routing, validation/rerouting, execution, grounding, individual timings, and cumulative runtime.

Raw model chain-of-thought is never exposed. The visible reasoning text is a short structured execution plan plus observable harness stages (tool discovery/synthesis, validation, repair, execution, and verification).
