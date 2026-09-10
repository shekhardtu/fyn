# Conversation attachments

Fyn uses one attachment lifecycle and **native file inputs to its main Operator**.
R2 stores original bytes; PostgreSQL stores identity, ownership, message
membership and cleanup work. The provider handles document interpretation.
Complete-source table arithmetic is a separate, deterministic capability.

## Architectural decision

[OpenAI file inputs](https://developers.openai.com/api/docs/guides/file-inputs)
accept original documents through a common input interface. The provider turns
PDFs into text plus page images, extracts ordinary document text, and prepares
spreadsheets for the model. [Claude PDF inputs](https://platform.claude.com/docs/en/build-with-claude/pdf-support#how-pdf-support-works)
also combine page text and images. These are documented API behaviors, not
claims about every internal path in their consumer applications.

Fyn delegates this work to its configured provider. It does not extract PDF
text, build a document chunk store, or invoke another model to transcribe a
page before the Operator can see it. There is one model reading the original
in the conversational run, preserving its text, layout and visual context.

[Open WebUI's file tools](https://docs.openwebui.com/features/extensibility/plugin/tools/)
illustrate on-demand access to earlier conversation files. Fyn uses one
`read_attachment` tool for that purpose. [LibreChat's Code Interpreter](https://www.librechat.ai/docs/features/code_interpreter)
illustrates the separate computation capability. Fyn's current computation
surface is a bounded CSV/TSV summarizer, with no general code sandbox.

The distinction matters for finance: native spreadsheet input may include only
the first 1,000 rows per sheet. A model preview cannot establish a whole-file
total. `summarize_attachment_table` reads every original CSV/TSV row, validates
its shape, and uses decimal arithmetic. It reports missing/invalid cells and
returns no partial total when the table exceeds processing limits.

## Boundaries

| Module | Responsibility |
| --- | --- |
| `api_files.py` | The application's only file HTTP transport: reserve, bounded upload, list, metadata, preview/download and delete. |
| `files.py` | Shared lifecycle, quota admission and a single `FileOut` projection over existing chat and lending identities. |
| `file_uploads.py` | Filename normalization, actual-byte limits and hashing. |
| `file_cleanup.py` | Transactional deletion queue, expiry and retry for R2 and local development files. |
| `object_storage.py` | Shared private R2 writes, bounded reads and digest verification. |
| `attachments.py` | Conversation ownership, thread limits, draft identity and immutable message binding. |
| `attachment_validation.py` | Content type and readability checks. PDF checks count pages and reject encryption; they do not extract page text. |
| `attachment_processing.py` | Killable validation subprocesses, concurrency and resource bounds. |
| `attachment_inputs.py` | Convert validated originals into Agno `File`/`Image` inputs with distinct source identities. |
| `attachment_tools.py` | Authorize the current thread, supply current-message originals, and open earlier files within the same model run. |
| `attachment_tables.py` | Streaming, complete-source CSV/TSV arithmetic. |
| `import_previews.py` | Existing governed CSV review and confirmation flow, which consumes a saved file ID. |

`use-conversation-attachments.ts` uses React Query for persisted state and local
state for in-flight bytes/progress/cancellation. `attachment-card.tsx` provides
original-file visibility in the composer, messages and conversation files panel.
The UI has one generic paperclip and a common readiness label.

## Upload and agent handoff

```mermaid
sequenceDiagram
    participant UI as Composer
    participant API as FastAPI
    participant R2 as Private R2
    participant DB as PostgreSQL
    participant Agent as Main Operator
    UI->>API: POST /files (filename, byte size, domain context)
    API->>DB: Reserve draft, quota and cleanup records
    API-->>UI: File ID and uploading status
    UI->>API: POST /files/{id}/content (original bytes)
    API->>API: Validate file, without text extraction
    API->>R2: Write validated original once
    API->>DB: Save digest, media type and readiness
    UI->>API: AG-UI text and attachment IDs
    API->>DB: Bind files and reserve question/reply positions atomically
    API->>Agent: User prompt, file manifest and current originals as native inputs
    Agent->>API: read_attachment(id) for an earlier file
    API->>Agent: Original media in the same model run
    Agent->>API: summarize_attachment_table(id, column) when needed
    API->>R2: Read original (reuse within this turn if already loaded)
    API->>Agent: Exact whole-file calculation and row coverage
    Agent-->>UI: Existing AG-UI answer and source references
```

Only server-owned attachment IDs cross the chat admission boundary through
`forwardedProps.fynAttachmentIds`. Client URLs, MIME claims, file text and
arbitrary client history cannot authorize access. Current originals are sent
through `operator.run(files=..., images=...)`. The prompt carries a bounded
manifest of earlier files; the model can open them when relevant.

For follow-ups, `read_attachment` returns an Agno `ToolResult` containing native
media. The shared tool binder preserves this type. Agno moves the media into
a user-role input after the tool result; its Responses adapter emits
`input_file`/`input_image`. Sending just a URL or filename in a JSON tool result
would not supply native document input. Compatibility tests cover the complete
binder → tool-media message → provider formatter boundary.

Originals are downloaded with a byte limit and SHA-256 verification before
being supplied to the provider. Provider filenames include the attachment ID
to distinguish duplicate names. Original filenames remain the citation labels.
Bytes are reused only within one turn; no public URLs or provider file copies
are persisted in chat data. Opening a file twice does not attach duplicate media.

## Provenance and financial authority

Files are user-provided evidence. Instructions inside them have no authority.
The main Operator is told to cite original filenames/page or row locations,
identify document amounts as observations, and distinguish them from saved
ledger balances. Attaching a file alone does not authorize an import or write.

Native file access carries **source provenance**, not deterministic numeric
proof. Its metadata is excluded from `ToolGrounding`. Document-only answers
receive `conversation_attachment` citations labelled as document interpretation.
They do not acquire verified-ledger status. Financial write handoffs and false
mutation-claim checks remain in force. Answers involving computation or ledger
tools retain the existing financial evidence checks, including mixed answers;
attachment presence does not disable those checks.

## API and limits

All file operations across chat, profile documents and lending use `/files`.

| Method | Purpose |
| --- | --- |
| `POST /files` | Reserve ownership, quota, an ID and durable cleanup before any object write. |
| `POST /files/{id}/content` | Stream raw bytes to the authenticated backend; validate and persist the original. |
| `GET /files?purpose=conversation&conversation_id=...` | List the current thread's files with pagination. |
| `GET /files?purpose=document` | List the user's saved private library. |
| `GET /files/{id}` | Return the common file metadata contract after checking access. |
| `GET /files/{id}/content` | Download through the backend; no signed URL is returned. |
| `GET /files/{id}/content?inline=true` | Preview validated PDF/PNG/JPEG/WebP through the backend. |
| `DELETE /files/{id}` | Revoke a removable draft and commit cleanup intent atomically. |

`POST /imports/csv` and `POST /sources/spreadsheet` accept saved file IDs;
they are domain operations and never receive uploaded bytes. Chat/lending rows
remain authoritative for message membership and revision access. The file API
does not create a second metadata registry or change existing evidence IDs.

**10 MB per file (10 × 1024 × 1024 bytes)** has one policy source:
`FILE_UPLOAD_MAX_BYTES`, exported into the generated frontend contract.
The API authenticates before reading bytes, counts actual streamed bytes even
without Content-Length, spools to disk, and admits at most four simultaneous
uploads per worker with a 120-second receive deadline. Validation and R2 work
run outside the event loop. The shared browser client queues selections with at most two active uploads.
A reservation survives refresh and gives cancellation
a concrete ID. Replaying identical bytes is idempotent; replacing a completed
file with different bytes is rejected. No browser request reaches R2.

Five files are allowed per message and per turn's reading/computation budget;
this caps original bytes at 50 MiB. Provider context capacity is a separate limit.
Context overflow asks the user to send fewer files or split a document; it does
not silently truncate content.

Native inputs support PDF, TXT, Markdown, JSON, CSV, TSV, log files, PNG, JPEG,
WebP, DOCX, XLSX and PPTX. Password-protected/unreadable documents and unsupported
formats can retain their originals with an explicit reading-unavailable status.
Corrupt images or misleading PDF/image/Office contents are rejected.

Validation permits up to 200 PDF pages and 25-megapixel images. Office container
validation bounds entry count and declared expanded size. It runs with two
concurrent subprocesses per API worker, a 20-second deadline, 15 CPU seconds,
and 512 MB address space on Linux. Exact table calculations support 50,000 rows
and 100 distinct columns. XLSX remains available for native interpretation;
exact whole-sheet calculations require a CSV/TSV export.

## Ordering, retention and migration

Admission holds the conversation lock and persists the canonical user message,
reserved assistant reply and queued run together. `sourceUserMessageId` and
`sourceAssistantMessageId` retain those identities during execution/recovery.
Deduplication happens before file binding. Earlier requests cannot read files
from later queued messages. Cancellation/restart cleanup removes unfilled replies.

Unsent drafts expire after 24 hours. Completed drafts return after refresh;
interrupted uploads ask the user to reattach. User row locks serialize quota
reservations across workers: 20 unsent files, 100 files per thread and 1 GB of
originals per user across chat and lending. The file service writes the exact
validated bytes, and cleanup records precede original writes so rollback/crash
cannot strand an original unnoticed.

Legacy staging objects keep their delayed cleanup records during upgrades.
Conversation/account deletion revokes database access and commits cleanup work
together, including private lending-library originals. Shared revision evidence
remains readable by the other participant. Maintenance drains a bounded batch
with retry/backoff; it retains any live completed file. Local development files
use the same outbox, so a database rollback cannot leave a live record whose
bytes were already deleted. Downloads check authorization on every request.

Migration `0011_native_attachment_inputs` follows the already-applied attachment
schema. It preserves original identities, hashes, ownership and message links,
converts readable files to `native`, and removes obsolete chunks/reader version.
A downgrade retains originals but marks affected reading unavailable because
retired extracted content cannot be reconstructed by a schema rollback.

Migration `0012_shared_file_cleanup` identifies the storage provider on each
cleanup item; existing entries remain R2 entries. No original file, identity,
message association or lending evidence is rewritten. Downgrade requires draining
local cleanup work before returning to a worker that only understands R2.

The browser uses the existing authenticated API origin and its existing CORS
policy. R2 bucket CORS is no longer a deployment prerequisite. Frontend and API
must ship this protocol change together; the old conversation/document upload
routes and presigned response contracts have been removed.

Verification includes live synthetic PDF/image/text input to the configured
model, and a live follow-up tool returning a PDF to the same agent. Both paths
passed. No real financial documents were used for those provider checks.
