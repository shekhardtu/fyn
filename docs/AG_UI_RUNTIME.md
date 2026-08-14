# Fyn AG-UI runtime contract

Fyn uses AG-UI as its only interactive-agent transport. The migration is a hard cutover, not a feature-flagged compatibility layer.

## Client adapters

- Web keeps one official `@ag-ui/client` `HttpAgent` per thread. Its native message, state, pending-interrupt, lifecycle-verification, and capability-discovery behavior survives multiple runs and in-process reconnects.
- Expo uses native `expo/fetch` streaming and validates every event with the official `@ag-ui/core` discriminated schemas. Its reducer mirrors the official client for messages, tools, state snapshots/deltas, activities, reasoning, and pending interrupts. This adapter exists because AG-UI does not currently provide a React Native client.
- Fyn widgets remain the product's typed, versioned UI contract. Their validated result is carried in the namespaced `fyn.response.v1` custom event; protocol lifecycle and orchestration use standard AG-UI events.

## Run lifecycle

Every accepted command creates a persisted `agent_runs` row. Its ordered AG-UI events are appended to `agent_events`, and any human decision is represented by `agent_interrupts`.

```text
POST /api/agent
  → RUN_STARTED
  → STATE_SNAPSHOT (safe Fyn projection only)
  → ACTIVITY_SNAPSHOT*
  → REASONING_* (observable-stage summary, never raw chain of thought)
  → TEXT_MESSAGE_*
  → CUSTOM fyn.response.v1
  → TOOL_CALL_* + STATE_SNAPSHOT + MESSAGES_SNAPSHOT + RUN_FINISHED(interrupt)
     or STATE_DELTA + RUN_FINISHED(success)
     or RUN_ERROR
```

The event sequence is durable and monotonic per run. An event is committed before it is delivered live, and terminal events commit atomically with terminal run state. Leaving a thread or losing the network detaches the listener; it does not cancel the server run. Both clients retain the last replay-safe SSE sequence and continue from that cursor. A fresh client replays from the beginning; a connected client receives a verifier-compatible continuation beginning with a synthetic, non-persisted `RUN_STARTED` boundary.

At startup, queued runs are resumed. A run that was already executing is never replayed blindly because a governed action may have crossed its side-effect boundary before the process exited. Instead, recovery appends a durable `RUN_ERROR(code: "server_restart")`; reloading the canonical conversation shows any response that committed before the restart.

## Human-in-the-loop actions

A pending Fyn widget action becomes a standard tool-bound AG-UI interrupt. The proposed action is emitted as the complete tool arguments; the response schema uses the standard `approved` / `editedArgs` shape, and the resumed run emits the actual governed execution response as `TOOL_CALL_RESULT`. While an interrupt is open, the composer pauses and the existing widget is the primary response surface. If that widget is unavailable, both clients render a small design-system fallback that can approve, deny, or cancel without deadlocking the thread.

Resumption must address every open interrupt and is routed through the same typed payload validation, widget-origin checks, deterministic domain handler, lifecycle receipt, and audit behavior as any other Fyn action. Replaying the same `(threadId, interruptId, status, payload)` under a new run id returns the previously committed response without executing the action again. Non-resume input while an interrupt is open is accepted as a run and terminates with `RUN_ERROR(code: "pending_interrupt")`.

Secondary widget actions that do not represent a suspended run start a new AG-UI run through `forwardedProps.fynAction`; they are still validated and governed on the server. Client-provided tool declarations are ignored and are explicitly advertised as non-authoritative.

## State and authority

AG-UI state events contain only a safe interaction projection: thread id, run id, phase, message id, and interrupt ids. The ledger, balances, drafts, permissions, and other canonical financial state remain server-side. Client-supplied state, history, context, and tools cannot overwrite or authorize financial data.

The server accepts only one of three reduced commands from a run input:

1. the newest user text message;
2. a validated namespaced Fyn widget action; or
3. a validated AG-UI interrupt resume.

## Cancellation and ordering

Runs in one conversation form a persisted predecessor chain, so concurrent clients cannot race replies or mutations. Cancellation is cooperative: a queued run stops immediately; an executing run stops only at a safe harness boundary. If a governed financial operation has already completed, its verified response is returned rather than pretending the change did not occur.

## Capability boundary

The capabilities endpoint advertises what this implementation actually supports:

- streaming HTTP transport and event replay;
- structured output;
- state snapshots and JSON Patch deltas;
- persistent server memory/state;
- safe reasoning summaries (complete summaries, not token streaming);
- tool-call events and interrupt approvals with edits.

The clients query this declaration rather than assuming support. The server advertises sequence resumability because both clients consume replay-safe cursors, and advertises approve-with-edits because interrupt schemas accept the protocol's `editedArgs`. It does not advertise a maximum execution time because the underlying governed/model operations do not yet have a hard wall-clock kill boundary.

It deliberately reports unsupported capabilities as false: WebSocket, push notifications, binary HTTP, multimodal input/output, arbitrary code execution, and client-provided tool authority. Raw model chain of thought is never exposed. These can evolve independently without changing the core Fyn design system or weakening the finance authority boundary.
