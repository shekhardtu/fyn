# HITL component pattern

HITL is a workflow boundary, not a card style. A component is complete only
when its visual state and its server transition agree.

## Architecture

1. The backend owns every business transition. Each `pendingAction` must have
   a matching forward action and an explicit escape action before the response
   can be persisted.
2. Every actionable widget occurrence owns a unique event id. Resource ids
   belong in widget data and action payloads; reusing one as a later card id is
   rejected at persistence so completed state cannot leak into a new decision.
3. The conversation shell owns interrupt resumption. It supplies a protocol
   cancel fallback for older persisted widgets that predate an explicit cancel
   action.
4. Renderers own only interaction state: entered values, optimistic selection,
   validation, disclosure state, and focus.
5. Shared primitives own appearance: card, option, field, action row, error,
   and compact receipt. New HITL components should compose these primitives
   instead of creating local spacing or button rules.

## Lifecycle

```text
active ── choose/type ──> selected ── submit ──> pending ── success ──> completed receipt
  │                           │                       │
  │                           └── validation error ───┘
  ├── back/edit ──> previous valid active state
  └── cancel ──> cancelled receipt + composer unlocked

active ── newer turn ──> superseded receipt
pending ── request failure ──> active with the entered value preserved
```

The pending state disables every competing action but does not remove it or
change the card's geometry. Completion, cancellation, and supersession replace
the controls with one compact historical receipt.

## Transition audit

| Family | Forward transition | Revision transition | Escape transition |
| --- | --- | --- | --- |
| Clarification | choose an authored option or submit custom text | edit custom text before submit | cancel clarification |
| Amount/type/category/subcategory | submit the required value | return to the previous meaningful step | cancel transaction draft |
| Account | choose a saved account or enter a new account inline | change type/source account | cancel transaction draft |
| Taxonomy creation | add category/subcategory | back to selector | cancel transaction draft when nested; cancel when standalone |
| Transaction confirmation | save transaction | edit or change category | cancel transaction draft |
| Transaction edit | apply changes | back to confirmation | cancel transaction draft; cancel saved edit without mutation |
| Budget/goal/contribution | confirm budget create/update/delete or goal creation/contribution | edit a saved budget or start a new request after cancel | cancel pending action |
| Import review | import staged records | attach a corrected file after cancel | cancel pending action |
| Removal confirmation | remove transaction | cancel and keep transaction | cancel removal |
| Reconciliation | merge | go back from destructive confirmation | keep separate |
| Calculators | calculate/project | edit inputs and rerun | non-blocking; composer remains available |
| Dashboard tile removal | remove after an inline second decision | keep the tile | keep the tile |

## Visual rules

- One question lives in the assistant message. The card does not repeat it.
- Show supporting copy only when it changes the decision.
- Use a 12px card inset, 6–8px option gaps, and a 40px field box.
- Buttons and fields in the same row have the same visual height. Invisible hit
  slop expands compact controls to at least 44px without adding layout bulk.
- Order actions from escape/revision to primary commitment. Destructive final
  confirmation is visually distinct from ordinary cancellation.
- Empty state and entry control share one compact region; never display a large
  placeholder panel for an empty option list.
- Use 110ms state transitions and the shared enter/reveal motion. Respect
  reduced-motion preferences and never animate layout height during typing.
- Focus the newly active HITL boundary. Opening a custom input moves focus into
  it. Errors remain next to the invalid field.

## Required tests for a new HITL component

- Its backend response passes the blocking-contract invariant.
- Every action submits its server-authored id and payload.
- A later actionable card cannot reuse an earlier event id, even for the same
  resource.
- Empty data still has a completion path.
- Back and cancel reach different, correct states when both are meaningful.
- Pending preserves layout, indicates the chosen action, and prevents repeats.
- A failed request restores interaction without losing entered values.
- Completed, cancelled, and superseded states render compact receipts.
- Keyboard focus, accessible name, disabled state, and 44px effective targets
  are verified.
- Optional model-generated garnish is skipped while a HITL card is pending, so
  it cannot delay the decision surface.
