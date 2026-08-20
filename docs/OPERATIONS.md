# Filesystem operations

Fyn operations are loaded from files. PostgreSQL is not an operation registry:
it stores ordinary run, approval, and audit state, but never operation files,
compiled definitions, instructions, or revision history.

## Sources and reloads

- `backend/operations/common.yaml` contains protected instructions inherited by every operation.
- `backend/operations/core/` contains the protected built-in capability files.
- `OPERATIONS_MANAGED_DIR` points to the shared directory controlled by Ops.
- `.yaml`, `.yml`, and `.json` files use the same strict schema.
- Each API process loads the complete catalog at startup and watches it at runtime.

A reload compiles every file before atomically replacing the in-memory snapshot.
One invalid file leaves the last-known-good catalog active and changes `/health`
to `degraded`. A configured managed directory that is not mounted fails startup.
The health payload reports hashes and counts, not file content.

Definitions are deployment-wide, not user-owned. The authenticated user and
database session are injected only when an operation executes, so categories,
transactions, and every other resulting record remain scoped to that account.
This first version lets Ops publish reviewed files; end users cannot publish
their own operations or runbooks yet.

## Add an operation

Copy `backend/operations/template.operation.yaml.example` into the managed
directory and give it a `.yaml` suffix. That one file owns the operation's:

- identity, version, description, aliases, examples, and negative examples;
- input schema and missing-input wording;
- operation-specific model instructions;
- whether the model may select it and the bounded engine route it compiles to;
- primitive sequence and bounded `forEach` steps;
- expected effect, permissions, and approval copy;
- success/failure presentation and embedded end-to-end contract journeys.

No enum, router, API handler, prompt file, frontend component, or registry index
is added for an operation that uses existing primitives.

Validate before publishing:

```bash
cd backend
../.venv/bin/python -m app.operations validate /path/to/operation.yaml
../.venv/bin/python -m app.operations explain /path/to/operation.yaml
../.venv/bin/python -m app.operations test /path/to/operation.yaml
../.venv/bin/python -m app.operations primitives
../.venv/bin/python -m app.operations validate-catalog
../.venv/bin/python -m app.operations test-catalog
../.venv/bin/python -m app.operations model-eval-catalog --operation ops.taxonomy.create_path --include-negative
```

`test` is more than a YAML syntax check. Every positive embedded journey is
validated against the operation input schema, compiled through its declared
route, and dry-run through the real typed primitive workflow. For every
`discovery.modelSelectable: true` operation it also compiles the provider tool
and proves that the tool is strict, closed, and requires every declared field
(unknown values are represented as nullable rather than omitted). Negative
examples test the discovery boundary. `test-catalog` additionally checks that
every positive selectable journey survives bounded candidate retrieval.

Use three verification levels before publishing:

1. `validate` and `test` while authoring one file;
2. `validate-catalog` and `test-catalog` in CI before deployment;
3. API/browser journeys for the user-visible form, approval, cancellation,
   execution result, and user-account isolation.

The file-level journeys are deterministic and should run on every change. A
separate model-selection eval should run against the deployed model before a
model or prompt upgrade. `model-eval-catalog` executes those file-owned examples
through the real Operator and exits non-zero on a wrong selection; use
`--operation` for one capability during authoring and omit it for the complete
selectable catalog. It measures whether realistic paraphrases select the
expected strict proposal tool and whether negative prompts avoid that operation.
Production telemetry should then monitor selection, missing-input,
approval, cancellation, execution, stale-revision, and failure rates by
operation revision. These layers answer different questions and should not be
collapsed into one score.

Publish with an atomic file replacement so watchers never read half a document.
Increase `metadata.version` for a meaningful change. The runtime checksum is the
authoritative change detector.

## Safety boundary

Managed files may compose only primitives marked Ops-authorable. A primitive is
trusted code with server-owned input/output types, effect, confirmation,
permission, transaction, idempotency, and user-scope rules. Files cannot contain
code, SQL, shell commands, external URLs, credentials, arbitrary expressions,
parallelism, or nested workflows.

The compiler derives effect, permission, and minimum confirmation from the
referenced primitives. A file can strengthen confirmation but cannot downgrade
those derived rules. User identity and database sessions are injected at runtime
and cannot appear in operation input.

If Ops needs a genuinely new kind of system access, engineering adds one reusable
governed primitive. Ops can then compose it from any number of one-file operations.

## Scale and model context

The compiler turns each eligible core or managed operation into a separate, strict
proposal tool. Its name, description, and JSON Schema come from that one file;
operation identity, version, and checksum are bound by the server and are never
arguments the model can invent. A proposal tool cannot perform a read or write.
It stops the model turn and hands a typed candidate to the policy, approval, and
execution engine.

For a catalog no larger than `OPERATION_CANDIDATE_LIMIT`, every eligible tool is
shown to the Operator. This deliberately avoids a hidden verb gate: the model
can compare the full meanings even when the customer's wording and the YAML do
not share a token. For a larger catalog, the immutable discovery index retrieves
and ranks a bounded candidate set from descriptions, aliases, and examples, so
the model context does not grow with the catalog. The index is derived in memory
from the files and is not a second operation registry.

The selection sequence is therefore:

1. retrieve a bounded set of relevant file-owned operations;
2. let the model select at most one operation-specific strict proposal tool;
3. validate its inputs against the original file schema;
4. bind the current server-owned revision and reject stale proposals;
5. collect missing input, apply approval, and execute trusted primitives.

An unrecognized operation ID cannot reach the engine because the model never
authors one. A model outage or malformed contract fails closed; a number in an
unresolved read request is never reinterpreted as a transaction amount.

The generated proposal schema is a provider-compatible projection. Constraints
that strict model tools cannot express remain in the original file schema and
are enforced again by the server before a form, approval, or execution. The
provider contract is therefore helpful for extraction but never the final
authority for correctness.

This v1 intentionally recompiles the full directory to make a reload atomic; it
is not a claim that one process should parse a literal million files. At that
size, shard deployments by domain behind a deterministic catalog-routing tier,
giving each process pool its own managed root. Files remain the authoritative
registry and model prompts remain bounded in that topology.

## Approval changes

An approval stores only operation ID, version, checksum, validated user input,
and normal run state. On resume, the server reads the current in-memory catalog.
If the file changed, was disabled, or disappeared, the previous approval never
executes. Fyn either shows the current version for fresh approval or stops safely.

The old YAML or compiled plan is not copied into the database.

## Authoritative execution

The filesystem registry is the only registry of executable agent operations.
Every protected core capability and every managed operation must compile to one
or more `steps`; the old `adapter:` shortcut and the former non-strict generic
handoff tool are rejected by architecture tests. Model-selectable operations are
offered only as operation-specific strict tools. Engine protocol such as a plain
conversation reply, authenticated tool evidence, cancellation, and model-outage
recovery remains trusted source code because it is not an Ops-authored operation.

Core files may invoke protected runtime primitives owned by engineering. Managed
files may invoke only Ops-authorable primitives. Both go through the same
reference resolution, typed argument validation, effect derivation, workflow
loop, and last-known-good catalog snapshot.

## Source-code boundary

Application source contains the generic catalog compiler, workflow executor,
approval/resume engine, typed primitive implementations, and user-ownership
guards. It does not contain a hand-maintained capability list, effect table,
metric-to-capability table, safe-read set, mutation set, or alternate operation
router. Those values are derived from the protected operation files.

The closed capability enum used by typed model contracts is generated from the
protected files when a process starts. Policy and metric lookups read the live
catalog snapshot, so successful watched edits become authoritative immediately.
Adding, removing, or renaming a protected core capability changes the process's
typed contract and therefore requires a restart. The watcher rejects such a
topology change and retains the last-known-good snapshot. Managed operations use
the generic managed-operation capability, so their files can be added, changed,
disabled, or removed through the watcher without source wiring or a restart.

Every protected primitive must be referenced by at least one protected operation
file, and protected files may reference no undeclared primitive. The catalog also
rejects duplicate metric bindings. These startup/reload invariants turn missing
wiring into a visible catalog failure instead of a runtime hallucination.

Widget commands such as approval, cancellation, and form submission are engine
protocol, not Ops capabilities. Their typed handlers remain trusted source code;
the operation file owns which primitives run, their inputs, effects, permissions,
confirmation floor, and presentation. A new Ops operation that composes existing
Ops-authorable primitives requires only its one YAML or JSON file. A genuinely
new kind of system access still requires engineering to add and secure one new
primitive first.
