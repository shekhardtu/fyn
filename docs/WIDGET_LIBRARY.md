# Widget creation library

The copilot uses a hybrid response contract:

- `message` is safe Markdown for narrative, explanations, and compact lists.
- `widgets` are typed, versioned contracts for trusted data, charts, forms, and human decisions.
- Financial mutations are backend capabilities. Markdown and the model cannot create executable actions.

## Flow

```text
Agent/query plan
  -> governed domain result
  -> presentation blueprint
  -> WidgetLibrary validation
  -> versioned widget JSON
  -> frontend widget registry
  -> reusable renderer
```

`backend/app/services/widget_library.py` contains the creation layer. A `TableBlueprint` describes semantic fields and backend-authorized row capabilities. The library omits unpopulated columns, preserves field types, and validates the payload through Pydantic before persistence.

`frontend/src/components/widget-library/data-table.tsx` is the generic responsive renderer. It formats money, dates, percentages, states, and tags from declared semantics. It never guesses that a string is money and it only exposes actions present in each row's capability set.

`frontend/src/components/widget-renderer.tsx` contains the registered component catalog. Unknown widget code cannot be supplied by an agent.

## Adding a business use case

1. Retrieve and authorize the domain data outside the model.
2. Flatten the display rows and keep stable entity IDs.
3. Declare `FieldPresentation` metadata from the domain or governed semantic schema.
4. Declare `RowCapability` entries only for actions the current user may invoke.
5. Create the widget through `WidgetLibrary.data_table()`.
6. Add or reuse a registered frontend renderer and validate both backend and frontend schemas.

Use a specialized widget when interaction needs more than a table (confirmation, reconciliation, loan scenario, chart, or form). Reuse display primitives rather than accepting arbitrary React or HTML.
