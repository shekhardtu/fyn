# Co-pilot pattern library — open-source foundation references

The single source for every comparison claim a Co-pilot suggestion makes.
A suggestion may only cite entries from this file. Each entry needs at least
one open-source reference; `pinned` upgrades from a repo/mechanism claim to a
commit permalink once verified (Phase 1 includes the pinning pass). Entries
come from model knowledge through Jan 2026 until pinned — treat `pinned: no`
as "verify before quoting file paths".

Maintenance rule: adding a claim without a repo reference is not allowed;
prefer fewer, verified entries over many unverified ones.

---

## structured-output-retry
- **Claim**: Schema-validation failure on model output triggers a bounded retry with the error fed back, instead of crashing the run.
- **Refs**: `pydantic/pydantic-ai` — output validators + `ModelRetry`; `vercel/ai` — tool-call repair hook (`experimental_repairToolCall`).
- **Local relevance**: the `model_validate(result.content)` sites in `agents.py` (planner/validator/repair).
- pinned: no

## deterministic-guardrails
- **Claim**: Guardrails are typed, deterministic tripwires separated from model judgment; a probabilistic component never vetoes a machine-checkable invariant.
- **Refs**: `openai/openai-agents-python` — input/output `Guardrail` with tripwire results; `temporalio/temporal` — deterministic workflow code, side effects isolated in activities.
- **Local relevance**: `_deterministic_query_verification`, `_presentation_contract_issues`.
- pinned: no

## event-sourced-runs
- **Claim**: Durable, replayable event histories per run are the foundation for recovery, audit, and observation; consumers follow a cursor.
- **Refs**: `temporalio/temporal` — event-sourced workflow histories with deterministic replay; `langchain-ai/langgraph` — Postgres checkpointers per super-step.
- **Local relevance**: `agent_runs`/`agent_events`, `_agui_replay_response` cursor-follow.
- pinned: no

## hitl-interrupt-resume
- **Claim**: Human-in-the-loop pauses are declarative interrupt points with typed resume payloads, not prose negotiation.
- **Refs**: `langchain-ai/langgraph` — `interrupt()` / `Command(resume=...)`.
- **Local relevance**: `agent_interrupts`, the rename confirmation flow.
- pinned: no

## declarative-registries
- **Claim**: Capabilities, workflows, and rules are data with import-time invariants; adding behavior means adding a declaration, not code.
- **Refs**: `langgenius/dify` — file/DB-declared workflow nodes; `n8n-io/n8n` — declarative node definitions.
- **Local relevance**: `CAPABILITY_REGISTRY`, `AGENT_POLICIES`, the operations registry, this skill's detector list.
- pinned: no

## trace-evals-as-config
- **Claim**: Run-health checks are declared assertions evaluated over recorded traces, reproducibly — never ad-hoc judgment.
- **Refs**: `langfuse/langfuse` — evals over trace stores; `Arize-ai/phoenix` — OpenInference trace analysis; `promptfoo/promptfoo`, `confident-ai/deepeval` — assertions as config.
- **Local relevance**: the Co-pilot detector layer itself.
- pinned: no

## ambient-dev-context
- **Claim**: Ambient dev assistants stay useful by maintaining compact, refreshed context (repo maps, session digests) rather than streaming commentary.
- **Refs**: `Aider-AI/aider` — repo-map; `continuedev/continue`, `cline/cline` — context providers.
- **Local relevance**: the SessionStart digest + this skill.
- pinned: no

## grounded-follow-up-suggestions
- **Claim**: Follow-up suggestions are generated after the answer settles, from the finished Q&A, bounded to what the product can actually answer.
- **Refs**: `ag-ui-protocol/ag-ui` — the event/widget transport; `ItzCrazyKns/Perplexica` — open Perplexity-style related-question generation.
- **Local relevance**: `suggest_related_questions`, `capability_notes()`.
- pinned: no
