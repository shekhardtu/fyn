# Finance composer: capture, ask, act

Design and implementation review · 9 September 2026

[Desktop composer and effort menu](../output/composer/desktop-effort.png)

The composer is the starting point for three different jobs: capture a money
event, understand existing finances, and import financial records. Each needs
a clear moment when the user's data changes. Attachment starts with one generic
file picker: the application detects the format after selection, so the person
does not have to classify their upload first.

## Research informing the design

| Reference | Observed pattern | Application to fyn |
| --- | --- | --- |
| [Claude: model, effort, and thinking](https://support.claude.com/en/articles/8664678-change-the-model-effort-and-thinking-settings) | Effort is near Send, can change during a conversation, and trades response time for depth. | Auto / Quick / Thorough beside Send, described through financial tasks. Keep model names out of the everyday decision. |
| [Perplexity: interface navigation](https://www.perplexity.ai/cnr/hub/faq/interface-navigation) | Attachment and search-mode choices share the prompt area. | Keep one attachment action and an effort selector within the composer. |
| [Monarch: importing receipts](https://help.monarch.com/hc/en-us/articles/44244210547860-Importing-Receipts-into-Monarch) | Receipt capture leads to reviewable merchant, amount, date, and line-item details; receipts may match existing transactions. | Make staging, preview, and saving separate states. Future receipt intake must support matching existing entries as well as creating new ones. |

The fyn choices below are product recommendations inferred from these patterns
and the existing application, not claims that competitors implement this exact
design. Sources were reviewed for interaction patterns, not copied visually.

## What was getting in the way

- A paperclip disclosed only a CSV picker, with no explanation of the task it supported.
- Choosing a statement immediately started the import journey, without a distinct attachment state to inspect or remove.
- The main toolbar was occupied by an automatic-save explanation, leaving no visible shortcut for manual entry or control over effort.
- “Ask anything” did not teach the difference between asking a financial question and recording a transaction.
- The amount hint could interpret a year inside an analysis request as an entry amount.
- The floating scratchpad could overlap the mobile composer.

## Component anatomy

1. **Attachment tray**, shown when files are selected. Each file has a name,
   size, upload progress, reading status, preview, remove action, and retry on
   failure. No category selection is required.
2. **Writing area**, with a stable prompt: “Ask about your money, or describe a
   transaction…” It grows with the draft and supports pasted payment text.
3. **Action row**: a paperclip that directly opens the native file picker,
   visible Add transaction shortcut, effort selector, and Send / Stop.
   A saved CSV offers a separate Review transactions for import action.
4. **Consequence line** beneath the card: complete entries save when sent;
   attaching a file alone does not add transactions. Statements must be
   reviewed before import. Keyboard help appears where space permits.

The same composer serves the empty conversation and the docked conversation.
Dark and light appearances inherit the application's existing color tokens.
The transaction shortcut opens a sheet on phones and a centered dialog on
larger screens. The primary save action stays visible while its fields scroll.

## Implemented journeys

| Job | Entry and progression | Completion and recovery |
| --- | --- | --- |
| Ask about spending | Type a question → optionally choose effort → Send | Existing streamed response, Stop, and retry remain available. Failed message retries retain the original effort. |
| Record from natural language | Describe a payment or income → check the tentative amount hint → Send | Existing governed transaction intake handles completeness and classification. The hint is suppressed for questions. |
| Add a transaction manually | Add transaction → choose type → enter amount → optional merchant → check date and currency → Save transaction | Saves through the existing transaction API and refreshes transaction and overview caches. Chat stays intact. Closing and reopening retains the unsaved form within this composer session. Errors remain beside the entered fields. |
| Ask about documents | Paperclip, drop, or paste files → wait for upload and automatic reading status → add a question → Send | Original files remain in the message and conversation files panel. The agent reads relevant sections on demand and can use earlier files in follow-up questions. |
| Import a statement | Attach a CSV → Review transactions for import → inspect statement review → confirm | Existing duplicate handling and import receipt continue. The original stays attached to the review message. A written chat draft is preserved. Sending a CSV as context does not itself import it. |
| Use a payment note or receipt | Attach TXT, PDF, or image → check readiness → describe the intended task → Send | The main model reads the original through native file/image input and reports source locations and uncertainty. Financial writes still use the existing governed intake. |
| Recover an attachment | Upload fails → Retry, or remove the file | Completed unsent files return after refresh. An interrupted upload asks the person to reattach because the browser no longer holds its bytes. Removing a file preserves written text. |
| Paste from a payment app | Paste directly into the composer → check → Send | No clipboard permission prompt or automatic clipboard read. The person controls which text enters the conversation. |

Quick manual entry uses the existing transaction form and its currency, date,
and amount validation. Category, location, and other details can be added from
Transactions after saving. Transfers remain excluded from manual quick entry
because they require the existing account-aware transfer workflow.

## Effort semantics

| Choice | User-facing intent | Actual behavior |
| --- | --- | --- |
| Auto (default) | Everyday questions and money entries | Preserves the server's configured reasoning defaults. |
| Quick | Balances, totals, simple lookups | Low reasoning for the Operator and any analysis delegate. |
| Thorough | Spending patterns, comparisons, what-if planning | High reasoning for those agents; may take longer. |

Selection stays with the current conversation view and resets on switching
threads. It is persisted with each non-default message command for execution
recovery. It changes reasoning effort, never permission to change financial
records. Auto and Quick can behave identically when the server already uses
low reasoning. CSV imports do not use this selector. An interrupt resume is a
separate command and uses server defaults.

## Edge states and accessibility

- Empty message: Send disabled unless ready files are attached. Sending files
  alone asks what can be understood from them; it does not authorize an import.
- File selected: filename remains readable by tooltip; removing it does not clear typed text.
- The generic picker has no category or format filter. It accepts up to five
  files per message, each at most **10 MB**. Selection, drag/drop, and image
  paste share the same upload path.
- Empty and oversized files are rejected before upload. Server validation also
  enforces the limit and checks actual bytes. Unsupported formats can be saved
  with a clear reading-unavailable status; corrupt images are rejected.
- Uploading: per-file progress and removal remain available; Send waits for all
  files to be ready. The person can continue writing while files upload.
- Running: intake actions disabled, draft typing retained, Stop available.
- Interrupted: respond to the existing card; explicit CSV preview can begin the
  existing import workflow that supersedes the pending interaction.
- Enter sends, Shift+Enter inserts a line, IME composition does not submit.
- Base UI menus/dialogs manage arrow navigation, focus containment and return,
  and Escape. Controls have accessible names; progress and results are announced.
- The toolbar is fitted to narrow phones, and the scratchpad launcher sits above
  the measured composer dock.
- Written drafts are transient. Completed file drafts are persisted on the
  server for 24 hours; sent files remain until their conversation or account
  is deleted. Switching threads cancels that view's unfinished uploads.

## Document visibility and future reconciliation

Private R2 storage retains originals. The same attachment card opens from the
composer, a sent message, or the conversation files panel. Images have an inline
preview; PDFs have an authenticated Open PDF action; all originals can be
downloaded. Reading status remains visible when a file is encrypted or its
format is unsupported.

Receipt-to-ledger matching is a further product workflow. It should offer
**Match existing transaction** or **Add new transaction**, display extracted
merchant, amount, currency and date for correction, and preserve the source on
the chosen ledger record. The implemented attachment system supplies durable
source evidence and agent reading; it does not yet provide that dedicated
reconciliation interface.

## Evaluate the redesign

Observe task completion for entering a cash expense, pasting a UPI confirmation,
previewing a bank statement, and investigating a monthly spending change.
Measure time to successful entry, abandoned quick-entry drafts, attachment
validation failures, duplicate corrections, effort selections, and cancellations
while waiting. Use aggregate events without recording draft text or filenames.
Treat improvement targets as hypotheses until measured with actual users.


## Durable attachment architecture

The attachment implementation now uses private R2 storage, multi-file drafts,
persisted message cards and a conversation files panel. TXT is no longer
appended to the prompt, and selecting CSV no longer changes Send into Import.
A saved CSV offers an explicit review shortcut. The main model reads original
files directly; exact CSV/TSV calculations separately process every row. See
[ATTACHMENTS_ARCHITECTURE.md](ATTACHMENTS_ARCHITECTURE.md) for the current
lifecycle, endpoints, agent handoff, format support and 10 MB per-file limit.
