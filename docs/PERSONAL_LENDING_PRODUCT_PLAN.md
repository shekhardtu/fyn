# Personal lending product plan

Status: core end-to-end release implemented; creation and evidence journey expanded on 25 Aug 2026
Product area: Loans
Primary market: private loans between friends, family, colleagues, and other known contacts

## Current release checkpoint

The first usable vertical slice is complete. It includes privacy-bounded lookup by email or phone sign-in identifier, phone- and email-bound private invitations, mutual acknowledgement of immutable document revisions, explicit monthly or yearly interest with simple or compound calculation and visible bases, a reusable private document repository, lender-to-borrower evidence requests, optional descriptive assurance items, two-party payment confirmation, rate-limited reminders, two-party closure and assurance return, per-user portfolio projections, export/deletion handling, deterministic copilot metrics, and a transactional notification outbox.

The shared-record, participant/invitation, document/revision, notification, event-history, and idempotent-command components are independent domain modules. Personal lending configures and composes them; they are not coupled to lending vocabulary or calculations.

The current UI deliberately uses one return date and one optional assurance item to keep the first journey calm. Repository-backed PDF/JPG/PNG evidence is implemented; a handwritten signature image is not required by the default workflow because each verified participant submits an authenticated electronic acknowledgement against an exact revision and attachment manifest. Certificate-based external eSign, instalment schedules, comments/disputes, reminder preferences/quiet hours, and relationship-level people pages remain subsequent increments. The data and document boundaries below are designed to add those without rewriting the shipped flow.

### Creation and evidence gap closure — 25 Aug 2026

- Creation begins with intent, then changes every person, amount, date, and role label to lender or borrower language.
- Every step has an explicit validity gate. Clicking Continue, pressing Enter, or jumping through progress navigation cannot bypass a required field.
- Interest requires monthly or yearly frequency. Simple interest on fixed principal is the default; compound interest is available under an advanced control. Both show the calculated rupee effect before review.
- The lender can request required or optional evidence from the borrower without uploading it on the borrower’s behalf.
- Each profile has a private reusable document repository. Sharing creates a separate immutable agreement copy, while the private original remains reusable and invisible to the other participant.
- Fulfilling a borrower document request creates a replacement document revision. The borrower acknowledges its exact evidence manifest and the lender must independently review and acknowledge it.
- Every acknowledgement records the verified session, masked sign-in identity, time, timezone, a privacy-preserving HMAC fingerprint of the observed client IP, and a user-agent fingerprint. Raw IP addresses are not exposed in the shared UI or evidence export.
- The agreement workspace exposes all revisions. Either participant can open an older revision and inspect its terms, changes, files, acknowledgements, and evidence fingerprints.
- Plain-language UI and PDF copy says “Electronically acknowledged by …”; the obscure `/s/` convention is not used.

This closes the creation-flow gaps identified in the product review. Planned expansion items are tracked separately below and are not represented as shipped merely because the underlying modules can support them.

Verification completed for this checkpoint:

- 874 backend tests, including privacy-bounded contact lookup, API identity isolation, transactional rollback, idempotent retries, encrypted destinations, event-chain integrity, outbox recovery, projection rebuilds, privacy export/deletion, and email/phone journeys
- 573 frontend tests, strict type checking, lint with no errors, and a production build
- A repeatable Playwright portfolio-to-document test
- A live two-account browser journey from creation through invitation, acknowledgement, payment, assurance return, and closure, plus 412 px responsive checks with no horizontal overflow or browser console errors

## 1. Product goal

Enable two people who already know each other to create, mutually acknowledge, track, remind, revise, and close a personal loan with one trustworthy shared balance, whether either person starts with a phone number or an email address.

The product succeeds when both people can always answer:

- What amount was given or received?
- What repayment plan did we most recently agree to?
- Which payments are confirmed, pending, or disputed?
- What remains, and what is expected next?
- What changed, who changed it, and when?

The application is a neutral recordkeeping and reminder service. It does not match strangers, hold funds, guarantee recovery, decide disputes, present cheques, enforce collateral, or claim that an acknowledgement is automatically court-enforceable.

### Product promise

> One calm, shared record of money between people—clear to both sides, easy to update, and difficult to misunderstand.

### Modular construction standard

Build the feature as a composition of small product modules with stable contracts, in the same spirit that a UI library provides reusable primitives without deciding the application around them.

```text
Shared record
  ├── participants and channel invitations
  ├── document revisions and acknowledgements
  ├── notification outbox and delivery preferences
  └── append-only activity
          │
          └── Personal loan configuration
                ├── terms and repayment schedule
                ├── funding and payment cashflows
                ├── portfolio projection
                └── loan-specific language and permissions
```

The reusable modules know nothing about principal, interest, or repayment calculations. The loan module configures participant roles, required acknowledgements, document templates, reminder policies, and lifecycle transitions through typed application code.

Standards for every reusable module:

- Framework-neutral domain service and typed command/query contract
- Database-owned invariants and idempotency boundary
- No caller-supplied tenant or participant authority
- Configurable copy/policy passed as data, not hardcoded branching by product
- Accessible UI primitive with loading, empty, conflict, failure, and recovery states
- Generated backend/frontend contract from one source
- Focused unit tests plus at least one integration consumer
- No generic `utils`, untyped JSON escape hatch, or polymorphic foreign key without a validated aggregate root
- Backward-compatible evolution through explicit schema and contract versions

### Definition of success

- A first-time recipient can verify an invited phone number or email, understand the plan, and respond in under three minutes.
- Both parties see the same confirmed principal, payments, and outstanding balance.
- A retry, double-click, network timeout, worker restart, or duplicate provider webhook never creates a second financial effect.
- No agreed term is silently edited. A material change becomes a new proposal and requires the affected party's acknowledgement.
- A notification failure never rolls back or corrupts the loan record.
- Every displayed balance can be explained from immutable confirmed entries.

## 2. Product boundaries

### Included

- Money I gave and money I received
- Private and mutually shared records
- One-time and instalment repayment plans
- Zero-interest and optional-interest arrangements
- Counterparty invitations through phone or email
- Mutual acknowledgement and change proposals
- Disbursement and payment confirmation
- Gentle automatic and manual reminders
- Extension requests and revised schedules
- Optional attachments, assurance-item notes, and custody acknowledgements
- Disputes, comments, settlement, and closure
- Per-person and total lending/borrowing portfolios
- Financial-copilot reads, drafts, matching suggestions, and reminders

### Explicitly excluded

- Public borrower/lender discovery or marketplace matching
- Credit scoring and recommendations to lend to a person
- Wallets, escrow, pooled funds, or payment custody
- Platform guarantees or collection guarantees
- Automatic legal notices, cheque presentation, collateral seizure, or collateral sale
- Public defaulter labels, contact-book shaming, or messages to unrelated people
- Claims such as “verified borrower,” “legally secured,” or “court ready”

### Formality levels

Users choose the amount of structure appropriate to their relationship:

1. **Private note** — visible only to its creator.
2. **Shared record** — the counterparty verifies a channel and acknowledges the terms.
3. **Signed attachment** — the parties upload or sign a separate document.
4. **External agreement** — the parties attach a document prepared outside Fyn.

The default is Shared record. Higher formality is available but never presented as necessary for an ordinary friend or family arrangement.

## 3. Trust and UX principles

### Neutral language

Use “Money I gave,” “Money I received,” “repayment plan,” “remaining,” and “needs a response.” Reserve legal or collections terminology for user-uploaded documents.

Prefer:

- “Rahul has not confirmed this payment yet.”
- “The expected date passed five days ago.”
- “Ask Rahul for an update.”

Avoid:

- “Debtor,” “defaulter,” “delinquent,” “final warning,” or “legal action.”
- Red danger styling for an ordinary late payment.
- Countdown timers, artificial urgency, or threatening copy.

### Exact authenticity claims

Badges must state only what Fyn actually knows:

- “Phone verified” means the person proved control of that phone number.
- “Email verified” means the person proved control of that email address.
- “Agreed on 24 Aug at 14:32 IST” means that authenticated account accepted that exact term version.
- It does not mean that Fyn verified a government identity, ability to repay, or legal capacity.

### One shared truth, with visible uncertainty

- Confirmed events affect the shared balance.
- Proposed payments appear separately as “Awaiting confirmation.”
- Disputed events never disappear; they remain visible with their state.
- The interface explains how every balance was calculated.
- Neither party can edit or delete the other party's history.

### Calm control

- Every consequential action has a plain-language preview.
- Drafts are freely editable; acknowledged terms are versioned.
- A recipient can suggest changes without rejecting the relationship.
- Reminders have frequency limits, quiet hours, snooze, and dispute-aware pausing.
- Both parties can download their record at any time.

## 4. Information architecture

Add **Loans** to the existing Money navigation.

```text
/loans                         Portfolio: gave, received, upcoming, needs response
/loans/new                     Five-step creation flow (currently presented as a full-screen drawer)
/loans/:loanId                 Shared loan workspace
/loans/:loanId/activity        Complete human-readable history
/loans/:loanId/settings        Reminder and visibility preferences
/people/:personId/loans        Relationship portfolio
/loan-invitation               Clean URL after one-time invite-token exchange
```

On mobile, Loans is a first-level Money destination. Inside a loan, use a single scrollable workspace with anchored sections rather than many nested tabs. Desktop may expose Activity and Settings as secondary routes.

### Loans overview

```text
┌──────────────────────────────────────┐
│ Loans                         + New  │
│                                      │
│  Money I gave      Money I received  │
│  ₹84,250           ₹20,000           │
│                                      │
│  Needs your response                 │
│  Rahul recorded ₹5,000        Review │
│                                      │
│  Upcoming                            │
│  Rahul        ₹9,200       5 Sep     │
│  Priya        ₹4,000      12 Sep     │
│                                      │
│  People                              │
│  Rahul  ₹20,000 remaining  On track  │
└──────────────────────────────────────┘
```

Do not lead with charts. Lead with the balance, the next expected event, and anything needing the user's response.

### Shared loan workspace

```text
┌──────────────────────────────────────┐
│ Rahul • phone verified              │
│ You gave ₹50,000                     │
│                                      │
│ ₹20,000 remaining                    │
│ ████████████░░░░ 60% returned        │
│ Next expected: ₹5,000 on 5 Sep       │
│                                      │
│ [Record payment] [Send reminder]     │
│                                      │
│ Repayment plan                       │
│ Activity                             │
│ Attachments & assurance items        │
│ Reminder preferences                 │
└──────────────────────────────────────┘
```

The header always states the relationship from the current user's perspective. The same record says “You received ₹50,000 from Hari” for Rahul.

### New-loan flow

Keep creation to five understandable steps:

1. **Intent** — already gave/received, offering to lend, or requesting to borrow.
2. **Person** — the other person’s email or phone sign-in identifier first, followed by a matched or entered name.
3. **Terms** — contextual amount and date labels, one return date, optional interest, and optional note.
4. **Documents** — lender requests from the borrower, optional repository files shared by the creator, and an optional assurance item.
5. **Review** — exact summary, what the recipient will see or must provide, and “Acknowledge and send.”

Optional evidence and assurance details never obstruct the ordinary path unless the creator explicitly marks a borrower document request as required.

For interest, require the period and calculation method and show the total rupee effect. Never accept a bare “3%.”

## 5. Core user journeys

### A. Hari creates and Rahul joins by email

1. Hari signs in by phone.
2. Hari chooses “I gave money,” enters Rahul's name and email, and defines the plan.
3. Fyn commits the draft and invitation atomically, then queues the email after commit.
4. Rahul opens the email. Fyn exchanges the one-time token and removes it from browser history.
5. Rahul verifies the invited email. An existing account is used if that verified email is already linked; otherwise the existing passwordless flow creates an account.
6. Rahul sees the sender, amount, expected dates, optional interest, and what Fyn does and does not provide.
7. Rahul agrees or proposes changes.
8. On agreement, both portfolios show the same active plan.

### B. Invitation by phone

The same journey uses an E.164-normalized phone number and an SMS template. The SMS should avoid sensitive detail on a locked screen:

> Hari shared a private money record with you on Fyn. Review securely: fyn.example/i/…

The amount appears only after the recipient proves control of the invited number and enters the authenticated application.

### C. Email and phone belong to the same person

Do not infer that an email and phone belong together merely because the creator typed both. One verified invitation identity claims the participant slot. The recipient can later link the second identity through the existing profile flow. If the identities already belong to different Fyn accounts, do not merge them and do not reveal that conflict to the inviter.

### D. Record money that already moved

Hari may create a record after giving cash or making a bank transfer. Rahul separately acknowledges receipt. Until then, the funding item is “Recorded by Hari; awaiting Rahul.” The loan may be tracked privately if Rahul never joins, but it is never labelled mutually acknowledged.

### E. Record a repayment

1. Either party records amount, date, method, and optional proof.
2. The other party receives a review request.
3. Until confirmation, the proposed payment does not change the shared confirmed balance.
4. Confirmation allocates principal, interest, and fees according to the accepted plan in one database transaction.
5. Both user-scoped financial portfolios are refreshed from the same shared event.

For trusted relationships, a per-loan preference may allow “Automatically accept payments I record against my own bank transaction.” This remains off by default and cannot accept a change in amount or terms.

### F. Ask for more time

The recipient chooses “Request a new date,” proposes a date or schedule, and adds an optional note. The old plan remains active until the other party accepts. Acceptance creates a new immutable term version; rejection leaves the prior plan unchanged.

### G. Reminder

A reminder is a communication, not a financial mutation. It records requester, recipient, channel, template, scheduled time, delivery state, and related due item. A reminder request is committed before delivery is attempted.

If a loan or payment is disputed, automatic reminders pause. A user may send a neutral “Can we resolve this record?” message within frequency limits.

### H. Settlement and closure

When the confirmed balance reaches zero, either party proposes closure. Both confirm:

- No amount remains, or a specifically recorded amount was waived.
- Any assurance item or cheque was returned, destroyed, or otherwise resolved.
- The plan is closed.

Closure is an event, not deletion. Each participant may hide a closed loan from their default view.

## 6. Email, SMS, and in-app notification design

### Channels

- Email: Postmark using dedicated transactional templates.
- SMS: MSG91 using dedicated DLT-approved templates.
- In-app: canonical notification inbox and response queue.
- WhatsApp and push notifications are later adapters, not MVP dependencies.

Do not reuse OTP messages or OTP delivery records for loan communications. Reuse provider configuration and normalization conventions through a separate notification service.

### Message families

- Invitation
- Terms accepted
- Changes proposed
- Funding acknowledgement requested
- Payment confirmation requested
- Upcoming expected payment
- Expected date passed
- Extension requested or answered
- Dispute opened or resolved
- Closure requested or completed
- Assurance item return requested or confirmed

### Email anatomy

- Recognizable sender name and authenticated Fyn domain
- Subject naming the person and action, not a legal threat
- Preheader explaining why the recipient received it
- Minimal summary
- One primary action
- Plain-text fallback link
- “This is a private recordkeeping reminder; Fyn does not hold funds or decide disputes.”
- Notification preference and abuse-report links

Example subject:

> Hari shared a ₹25,000 repayment plan with you

### Delivery rules

- A domain event and outbox row commit together.
- Workers deliver after commit with exponential backoff and bounded attempts.
- Provider message IDs are unique and webhook events are idempotent.
- A failed delivery is visible to the sender without changing the financial state.
- Use local dates and each recipient's timezone for scheduled communication.
- Apply user quiet hours and per-loan frequency caps.
- Stop future automatic reminders when closed, cancelled, disputed, revoked, or opted out.

## 7. Domain state machines

### Loan lifecycle

```text
draft
  → pending_acceptance
  → active
  → settlement_pending
  → closed

pending_acceptance → changes_requested → pending_acceptance
draft|pending_acceptance → cancelled
active|settlement_pending → disputed → active|closed
```

Funding is separate from lifecycle:

```text
not_recorded → pending_confirmation → confirmed
                                 ↘ disputed
```

### Term version

```text
draft → proposed → accepted → superseded
                 ↘ rejected
```

### Payment

```text
proposed → confirmed
        ↘ disputed → confirmed|voided
```

### Invitation

```text
pending → exchanged → redeemed
       ↘ expired
       ↘ revoked
```

### Reminder

```text
scheduled → queued → sent → delivered
                   ↘ failed → queued|dead_letter
scheduled|queued → cancelled
```

Every transition is authorized by participant role and current state in deterministic domain code. The client and copilot may request transitions but cannot decide their validity.

## 8. Data architecture

### Architectural decision

The existing `loans` table is a user-owned snapshot designed for personal planning and institutional liabilities. It cannot be the shared source of truth because two user-owned copies can drift and its `user_id` ownership cannot express participant access.

Introduce a neutral shared-loan domain. Keep `loans` as a user-scoped, rebuildable portfolio projection and as the compatibility home for existing private loan rows.

Shared agreement tables are not exposed to arbitrary model-authored SQL in the first release. The copilot reads curated, participant-authorized projections. This preserves the existing tenant SQL boundary.

### Canonical tables

#### `loan_agreements`

- `id`
- `created_by_user_id`, nullable with `ON DELETE SET NULL`
- `status`
- `currency`
- `current_term_version_id`
- `row_version` for optimistic concurrency
- `funding_status`
- `opened_at`, `closed_at`, timestamps

No mutable cached balance is treated as canonical here.

#### `loan_participants`

- `agreement_id`
- `role`: lender or borrower in V1
- `user_id`, nullable until claimed and detachable on account deletion
- `display_name_snapshot`
- `invited_channel`
- `verification_claim`: phone/email control only
- `claimed_at`, `hidden_at`

Constraints ensure one lender and one borrower participant per V1 agreement. API reads always require an active participant row; a caller-supplied user ID is never trusted.

#### `loan_invitations`

- `participant_id`
- `channel`
- normalized destination hash for lookup/deduplication
- encrypted destination for delivery
- one-time token hash; never store the raw token
- `expires_at`, `exchanged_at`, `redeemed_at`, `revoked_at`
- send counters and last delivery state

After the browser exchanges a token, immediately replace the URL and use a short-lived server-side invitation session. Set a strict referrer policy so invite secrets do not leak to third-party resources.

#### `loan_term_versions`

- `agreement_id`, monotonically increasing `version`
- principal in integer minor units
- disbursement date
- repayment style
- interest mode, annualized decimal rate, calculation basis, and rounding policy
- grace period and optional late-term fields
- free-text purpose/note
- canonical JSON snapshot and SHA-256 hash
- accepted document revision link and source-snapshot hash
- proposer and proposal/acceptance timestamps

Term versions are immutable after proposal. A unique `(agreement_id, version)` constraint prevents races.

#### `loan_term_acceptances`

- `term_version_id`
- `participant_id`
- authenticated actor
- accepted hash
- channel and timestamp

Unique `(term_version_id, participant_id)` makes acceptance idempotent.

#### `loan_schedule_items`

- `term_version_id`
- sequence
- local due date
- principal, interest, fee, and total minor units
- rounding residue allocation

The deterministic calculation engine generates these rows. Their sum must equal the accepted term version's calculated totals.

#### `loan_cashflows`

- `agreement_id`
- kind: disbursement, repayment, waiver, correction, or settlement
- direction participant IDs
- amount and currency
- principal/interest/fee breakdown
- occurred date and optional external reference digest
- initiated by, state, confirmed by, timestamps
- client idempotency key

Only confirmed cashflows affect the shared balance. Corrections reverse prior entries; rows are never overwritten.

#### `loan_payment_allocations`

- `cashflow_id`
- `schedule_item_id`
- principal, interest, and fee minor units

The allocation sum must equal the confirmed cashflow breakdown. Overpayment policy is explicit: reject, leave as unapplied credit, or settle; never silently produce a negative balance.

#### `loan_transaction_links`

- `user_id`
- `cashflow_id`
- optional existing `transactions.id`
- match source, confidence, and confirmation state

Each participant links the shared event to their own private bank or cash transaction. One person's bank details are not shared with the other. Principal remains a receivable/payable movement rather than spending or income; only interest and explicit fees contribute to income/expense analytics.

#### `loan_security_items` and `loan_custody_events`

Optional descriptive records for an assurance item, cheque, or external document. Store masked identifiers, attachments, who says they hold it, and return confirmation. No enforcement action exists.

#### `loan_reminders`

- agreement and optional schedule item
- requester and recipient participants
- manual or automatic source
- neutral template ID and user-supplied note
- scheduled time, state, dedupe key

#### `notification_outbox` and `notification_attempts`

The transactional outbox is the reliability boundary between financial commits and external providers. Attempts retain provider identifiers, safe error codes, and timing—not message bodies or OTP/security values in logs.

#### `loan_events`

Append-only human and machine audit history:

- agreement-local monotonically increasing sequence
- event type and schema version
- actor participant/user when applicable
- safe structured payload
- request/idempotency key
- occurred timestamp
- previous-event hash and event hash where evidence integrity is useful

Use relational tables as canonical domain truth plus an append-only event/audit stream; do not build a pure event-sourced system for the MVP.

### User-scoped projection

Extend the existing `loans` table with nullable shared-agreement linkage and direction metadata, or introduce a replacement `loan_positions` table and migrate the semantic registry deliberately. The projection contains:

- `user_id`
- `agreement_id`
- direction: lent or borrowed
- counterparty display label
- confirmed outstanding principal
- accrued/expected interest
- next due date and amount
- lifecycle and response-needed state
- last projected event sequence

It is updated in the same transaction as a confirmed domain action and can be rebuilt completely from canonical tables. A reconciliation job compares it with a fresh calculation and alerts on any drift.

### Account deletion and shared records

A jointly acknowledged record cannot be cascade-deleted merely because one account is deleted; that would destroy the other person's history. The acceptance screen and privacy policy must explain this shared-record property.

Proposed behavior:

- Unshared drafts and unredeemed invitations created solely by the deleting user are removed.
- For acknowledged shared records, detach the deleted `user_id`, revoke access and reminders, delete unnecessary destination/contact data, and retain only the minimized participant snapshot required for the other party's record.
- The remaining participant keeps the shared financial history but cannot contact the deleted account through Fyn.
- Final retention and erasure wording requires a privacy-policy review before release.

## 9. Independent document and revision module

### Why this is a separate module

Documents have a lifecycle that is related to, but different from, a loan's financial lifecycle. A loan owns amounts, schedules, balances, participants, and payment state. The document module owns human-readable content, editing provenance, proposals, comparisons, acknowledgements, signatures, rendering, and export.

Keeping these responsibilities separate provides three protections:

- An edit to prose cannot silently change the calculated loan balance.
- An accepted or signed document can never be overwritten by a later draft.
- The same document capability can later support payment receipts, assurance-item receipts, settlements, rent arrangements, or other financial acknowledgements.

Build this as an independent bounded module with a loans integration, not as an unbounded word processor. The first release should use structured sections and proposal-based editing rather than real-time Google Docs-style co-authoring.

### Source-of-truth rule

The Loans domain remains canonical for structured financial terms. The document revision is the exact human-readable representation accepted by the parties.

When a proposal is published:

1. Freeze the structured `loan_term_version`.
2. Compile the document from that exact term snapshot and any permitted custom clauses.
3. Store the term snapshot hash in the document revision.
4. Validate that every displayed amount, date, rate, and schedule total matches the structured source.
5. Freeze and hash the document revision.
6. Link the term version and document revision in the same transaction.

If a user edits a structured financial field inside the document experience, route the change through the loan-term editor and regenerate the calculated sections. Do not let free text override a structured value. A conflicting custom clause must block proposal publication and explain the conflict.

### Initial document types

- Repayment-plan acknowledgement
- Amendment or revised repayment plan
- Disbursement acknowledgement
- Payment receipt
- Assurance-item custody receipt
- Assurance-item return receipt
- Settlement and closure statement
- External uploaded agreement or supporting attachment

System-generated records and external uploads are clearly distinguished. Uploading a file does not mean Fyn verified its contents or signatures.

### Editing model

Use proposal-based collaboration:

```text
accepted revision N
       │
       ├── Hari creates change set A
       │      └── proposed revision N+1
       │
       └── Rahul creates change set B
              └── conflict/rebase if N+1 was accepted first

proposed revision → accepted by required parties → executed/final
                 ↘ rejected
                 ↘ withdrawn
```

- Autosave may update a private edit session, but it does not create shared truth.
- “Propose changes” creates an immutable revision based on a known base revision.
- The recipient sees a field-level and text-level comparison before responding.
- Acceptance records the exact revision hash, not merely the logical document ID.
- A stale acceptance against a superseded proposal is rejected.
- Comments do not change document content and can be resolved independently.
- A final revision is immutable. Corrections require another revision or a formal amendment.

### Amendment rule

An amendment never replaces or rewrites the original record.

An amendment contains:

- Reference to the prior accepted document and term version
- Proposed changes with before/after values
- Reason and optional note
- Effective date
- Parties whose acknowledgement is required
- New repayment schedule, when applicable
- Hashes of the prior and proposed revisions

When all required parties accept, one database transaction marks the amendment effective, makes the new term version current, supersedes the prior term version for future calculations, appends both loan and document events, updates projections, and queues notifications. Historical balances remain calculated under the versions effective at those times.

### Document data model

#### `documents`

- Logical document identity and type
- Status and current accepted/final revision
- Creator and timestamps
- Classification: system generated or external upload

The document itself is a container. It does not hold mutable authoritative content.

#### `loan_documents`

- Foreign keys to `loan_agreements` and `documents`
- Purpose within the loan
- Visibility and required-participant policy

Use an explicit foreign-key join rather than an unchecked polymorphic `entity_type/entity_id` pair. Future product domains can add their own binding tables.

#### `document_templates`

- Stable template key and semantic version
- Locale and content-schema version
- Structured block definition
- Allowed editable paths
- Renderer compatibility metadata
- Active/retired state

Template changes affect only new revisions. Existing revisions keep their original template and renderer identity.

#### `document_edit_sessions`

- Document and base revision
- Editing participant/user
- Private working content
- Optimistic version and expiry

These are recoverable drafts, not accepted history. Losing an edit session must never affect a proposed or accepted revision.

#### `document_change_sets`

- Document and base revision
- Author participant/user
- Structured patch operations with stable paths
- Before/after value hashes and a human-readable summary
- Created, proposed, withdrawn, and resolved timestamps

Store structured patches against a versioned JSON document model, not raw HTML diffs. User-provided rich text is sanitized at input and output.

#### `document_revisions`

- Document and monotonically increasing revision number
- Base revision and creating change set
- Immutable structured content snapshot
- Content-schema, template, and renderer versions
- Source financial snapshot hash
- SHA-256 content hash
- Author, creation reason, state, and timestamps

A unique `(document_id, revision_number)` constraint and optimistic base-revision check prevent last-write-wins data loss.

#### `document_comments`

- Revision and stable section/block path
- Author participant/user
- Body, created time, resolved time, and resolver

Comments are append-only except for an explicit edit window whose changes are audited. Deletion becomes a visible tombstone.

#### `document_acceptances`

- Exact revision ID and content hash
- Participant and authenticated actor
- Action: accept or reject
- Verified channel context and timestamp
- Optional reason for rejection

Unique acceptance per participant/revision makes retries safe.

#### `document_signatures`

- Exact revision and artifact hash
- Participant
- Signature method: acknowledgement, uploaded wet signature, or external e-sign provider
- Provider reference and verification result where applicable
- Signed time and revocation/failure metadata

An acknowledgement and an e-signature are different concepts and must appear differently in the UI.

#### `document_artifacts`

- Revision
- Artifact type: PDF, print view, source JSON, or uploaded original
- Object-store version/key, MIME type, byte length, and checksum
- Renderer version and generation state

Never regenerate an old “final PDF” in place. A renderer retry creates or verifies the artifact for the same immutable revision and is idempotent.

#### `document_amendment_links` and `document_events`

Amendment links explicitly connect prior and replacement revisions. Document events provide the append-only timeline for draft creation, proposal, view, comparison, comment, acceptance, signature, rendering, withdrawal, and amendment effectiveness.

### Document UI

Add a **Documents** section inside the shared loan workspace and use a reusable full-page document review route when focused reading is needed:

```text
/documents/:documentId
/documents/:documentId/revisions/:revisionId
/documents/:documentId/compare?from=…&to=…
```

The review screen prioritizes comprehension:

```text
┌──────────────────────────────────────┐
│ Repayment plan • Revision 3          │
│ Proposed by Rahul • 24 Aug, 2:32 PM  │
│                                      │
│ 2 changes from the version you agreed│
│                                      │
│ Return date                          │
│ 31 Aug  →  30 Sep                    │
│                                      │
│ Instalment                           │
│ ₹10,000 → ₹5,000 × 2                 │
│                                      │
│ Unchanged: principal, interest, note │
│                                      │
│ [Suggest another change] [Accept]    │
└──────────────────────────────────────┘
```

- Start with a plain-language change digest, then offer the complete document.
- Highlight insertions, removals, and structured field changes without relying on color alone.
- Show author and time for every change set, comment, acceptance, and signature.
- Show “Generated from the ₹50,000 plan, version 3” and expose calculation details.
- Permit download only for immutable proposed/accepted/final revisions; label draft exports clearly.
- On mobile, show the digest first and keep the complete revision readable without horizontal scrolling.

### Document APIs

```text
GET    /documents/{document_id}
GET    /documents/{document_id}/revisions
GET    /document-revisions/{revision_id}
GET    /documents/{document_id}/compare?from={revision_id}&to={revision_id}

POST   /documents/{document_id}/edit-sessions
PATCH  /document-edit-sessions/{session_id}
POST   /document-edit-sessions/{session_id}/change-sets
POST   /document-change-sets/{change_set_id}/propose
POST   /document-change-sets/{change_set_id}/withdraw

POST   /document-revisions/{revision_id}/comments
POST   /document-comments/{comment_id}/resolve
POST   /document-revisions/{revision_id}/accept
POST   /document-revisions/{revision_id}/reject
POST   /document-revisions/{revision_id}/signature-sessions

POST   /documents/{document_id}/amendments
POST   /document-revisions/{revision_id}/render
GET    /document-revisions/{revision_id}/artifacts
GET    /document-artifacts/{artifact_id}/download
```

The same participant membership resolver used by Loans protects bound documents. A user cannot gain document access from a guessed ID or an invitation to a different loan.

### Document integrity and recovery tests

- Accepted/final revision content is immutable at ORM, service, and API boundaries.
- Acceptance stores and revalidates the exact content hash.
- A modified payload with a reused idempotency key is rejected.
- Two users proposing from the same base produce an explicit conflict or rebase flow, never silent overwrite.
- An acceptance arriving after another revision became current is rejected as stale.
- Structured amounts, dates, rate, and schedule in the document exactly match the frozen loan term snapshot.
- A custom clause conflicting with structured terms blocks publication.
- Renderer failure leaves the proposed/accepted revision intact and retry produces one artifact identity.
- Tampered object-store bytes fail checksum verification and are not downloadable as verified artifacts.
- Email/SMS links open the exact revision named in the notification.
- Comments and change attribution remain visible after amendment or closure.
- Account deletion follows the parent shared-record policy without erasing the other participant's accepted revision.
- External uploads are type/size bounded, content-sniffed, malware-scanned, checksummed, and never treated as system-generated.
- Revision comparison works across template versions or gives a safe section-level fallback.
- Export includes the complete revision/amendment chain and never includes private edit sessions belonging only to the other party.

## 10. Transaction atomicity, integrity, and recovery

### Command transaction pattern

Every consequential endpoint follows one database transaction:

1. Authenticate and resolve participant membership.
2. Resolve the idempotency key. Return the prior receipt on replay.
3. Lock the agreement and affected schedule/payment rows with `SELECT … FOR UPDATE`.
4. Validate role, current state, currency, version, and financial invariants.
5. Insert immutable domain rows or reversal rows.
6. Update the user-scoped projections.
7. Append the loan event.
8. Insert notification outbox rows.
9. Commit once.

Provider delivery happens only after commit. No external network call occurs while financial rows are locked.

### Integrity controls

- Integer minor units for all money; `Numeric`/`Decimal` for rates.
- ISO currency per agreement; no mixed-currency cashflows.
- Positive principal and payment constraints.
- Unique idempotency keys scoped to actor and command type.
- Foreign keys for every participant, term, schedule, cashflow, and transaction link.
- Unique term versions and agreement-local event sequences.
- Optimistic `row_version`/`If-Match` for draft edits and pessimistic row locks for commits.
- No hard delete for accepted terms, confirmed cashflows, disputes, or closure.
- Reversal entries for corrections.
- Database-calculated or service-verified allocation totals.
- A deterministic balance function used by API responses, projection rebuilds, tests, and reconciliation.

### Recovery controls

- PostgreSQL automated backups and point-in-time recovery in production.
- Regular restore drills, not backup-success checks alone.
- Outbox workers claim rows with `FOR UPDATE SKIP LOCKED`.
- Exponential retry with dead-letter visibility and safe manual replay.
- Idempotent provider-webhook ingestion.
- Attachment checksums, object-store versioning, and orphan cleanup after a retention window.
- A `rebuild_loan_positions` administrative command that is safe to rerun.
- A `verify_loan_integrity` command that recomputes schedules, balances, event sequence, and projection parity without mutating records.
- Metrics for stale outbox rows, failed deliveries, projection drift, duplicate-command conflicts, and illegal-transition attempts.

## 11. API design

Follow the repository's current unversioned service-root API convention. Add a versioned root only when a future breaking contract requires it.

```text
GET    /loan-agreements
POST   /loan-agreements
GET    /loan-agreements/{agreement_id}
PATCH  /loan-agreements/{agreement_id}/draft
POST   /loan-agreements/{agreement_id}/cancel

POST   /loan-agreements/{agreement_id}/invitations
POST   /loan-invitations/exchange
POST   /loan-invitations/redeem
POST   /loan-invitations/{invitation_id}/resend
POST   /loan-invitations/{invitation_id}/revoke

POST   /loan-agreements/{agreement_id}/term-proposals
POST   /loan-term-versions/{version_id}/accept
POST   /loan-term-versions/{version_id}/reject

POST   /loan-agreements/{agreement_id}/disbursements
POST   /loan-cashflows/{cashflow_id}/confirm
POST   /loan-cashflows/{cashflow_id}/dispute
POST   /loan-cashflows/{cashflow_id}/void

POST   /loan-agreements/{agreement_id}/payments
POST   /loan-agreements/{agreement_id}/extension-requests
POST   /loan-agreements/{agreement_id}/reminders

POST   /loan-agreements/{agreement_id}/security-items
POST   /loan-security-items/{item_id}/custody-events
POST   /loan-security-items/{item_id}/return-confirmations

POST   /loan-agreements/{agreement_id}/closure-proposals
POST   /loan-closures/{closure_id}/confirm

GET    /loan-agreements/{agreement_id}/activity
GET    /loan-agreements/{agreement_id}/export
GET    /people/{person_id}/loan-summary
GET    /loan-notifications
PATCH  /loan-notification-preferences/{agreement_id}
```

Mutating requests carry `Idempotency-Key`. Draft updates carry the last observed version. Conflict responses distinguish stale version, invalid transition, participant mismatch, and already-completed action.

## 12. Financial copilot integration

The copilot operates through deterministic domain commands after the REST workflow is stable.

### Allowed without confirmation

- “How much have I given Rahul?”
- “What is due this month?”
- “Which payments need my response?”
- “Show the activity for my loan with Priya.”
- Deterministic repayment calculations and draft previews

### Requires preview and confirmation

- Create a draft plan
- Send an invitation
- Record a disbursement or payment
- Confirm or dispute a payment
- Propose a new date
- Send a manual reminder
- Propose or confirm closure

### Never delegated to the model

- Participant authorization
- Balance or schedule calculation
- Identity matching
- State-transition validity
- Idempotency
- Reminder rate limits
- Automatic acceptance of changed financial terms

Likely filesystem operation: `manage_personal_loan`, backed by narrow typed primitives rather than direct database writes. Chat widgets should reuse the same generated Pydantic/Zod contracts as the dedicated UI.

## 13. Implementation shape in this repository

### Backend

- Add domain enums and state-transition contracts in `backend/app/domain.py`.
- Add an Alembic revision after the current migration head for the shared tables, constraints, indexes, and analyst grants where appropriate.
- Add models in `backend/app/models.py` while preserving the frozen baseline migration.
- Add `backend/app/services/personal_loans.py` for deterministic commands and reads.
- Add `backend/app/services/loan_calculations.py` for versioned schedules and allocations, reusing common Decimal/minor-unit conventions from the existing calculators.
- Add `backend/app/services/loan_notifications.py` for outbox creation and delivery policy.
- Add `backend/app/services/loan_projections.py` for portfolio projection and rebuild verification.
- Add `backend/app/services/documents.py` for revision, comparison, acknowledgement, and artifact commands behind its own domain boundary.
- Add `backend/app/services/document_rendering.py` as a deterministic, retry-safe renderer adapter; rendering never owns financial mutations.
- Add a focused FastAPI router and include it in the existing API composition rather than enlarging unrelated route handlers.
- Extend export/deletion registries explicitly; the registry test must continue to fail for an unclassified user-owned model.
- Extend the semantic registry only with user-scoped projections, not raw shared tables.
- Add the copilot operation after deterministic service tests and REST contracts pass.

### Frontend

This is large enough for `frontend/src/features/loans/`.

- Add thin adapters in `frontend/src/routes/loan-routes.tsx`.
- Add reusable document review adapters in `frontend/src/routes/document-routes.tsx` and feature UI in `frontend/src/features/documents/`.
- Add route builders in `frontend/src/routing/paths.ts`.
- Add Loans to `MONEY_PAGES` in the workspace navigation.
- Keep server state in TanStack Query and transient creation-form state local to the feature.
- Extend backend-generated contracts and regenerate TypeScript/Zod artifacts; do not edit generated files.
- Build mobile-first screens at 320, 375, and 430 px before desktop refinement.
- Reuse existing visual primitives, typography, focus modality, overlays, toasts, and site header.
- Add route lifecycle tests using a memory router.

## 14. Delivery plan

Each phase ends in a demonstrable vertical slice and keeps the main test suite green.

### Phase 1 — Domain foundation

- Finalize terminology and state machines.
- Add migrations, constraints, repository/service boundary, calculation rules, idempotency receipts, event log, and user projection.
- Preserve existing one-sided loan data as legacy/private records.
- Deliver service-level create/read/rebuild tests before UI work.

### Phase 2 — Create, invite, and acknowledge

- Loans overview and four-step creation flow.
- Email and phone invitation adapters through the transactional outbox.
- Secure token exchange and existing passwordless identity claim.
- Recipient review, accept, reject, and propose-change flows.
- Structured acknowledgement document, immutable revision, revision comparison, and exact-hash acceptance.
- Both accounts see the same active record.

### Phase 3 — Funding, payments, and shared balance

- Funding acknowledgement.
- Payment proposal, confirmation, dispute, allocation, and reversal.
- Explainable balance breakdown and activity timeline.
- User-scoped transaction links and portfolio projections.
- Concurrency and replay test suite.

### Phase 4 — Reminders and communication preferences

- Upcoming, due, overdue, response-needed, and manual reminders.
- Quiet hours, cadence limits, snooze, per-loan channel preferences, delivery status, and dead-letter operations.
- Dispute/closure cancellation rules.

### Phase 5 — Extensions, optional assurance records, and closure

- Revised schedules through immutable term versions.
- Independent amendment documents linked atomically to the revised structured terms.
- Optional attachment and assurance-item custody timeline.
- Settlement, waiver/reversal rules, mutual closure, and return acknowledgement.
- Exportable human-readable record.

### Phase 6 — Financial copilot and analytics

- Curated loan read tools and deterministic mutation handoffs.
- Chat widgets for draft, response-needed, payment review, and reminder preview.
- Per-person portfolio, total receivable/payable, upcoming cashflow, and interest-only income/expense treatment.
- Bank/UPI transaction matching suggestions through existing reconciliation patterns.

### Phase 7 — Release hardening

- Accessibility, responsive, offline/retry, cross-browser, localization-ready copy, and performance passes.
- Privacy export/deletion and shared-record retention review.
- Restore drill, projection rebuild drill, notification retry drill, abuse-rate tests, and observability dashboards.
- Production provider templates, DLT configuration, bounce handling, and runbook updates.

## 15. Test strategy and acceptance matrix

### Domain and calculation tests

- Zero interest, simple interest, reducing balance, one due date, and instalments
- Required interest period/method; reject bare percentage
- Rounding across instalments; final schedule total exactly matches terms
- Partial payment allocation and configured allocation order
- Early payment, exact payment, overpayment, waiver, and reversal
- Leap year, month end, local date, and timezone boundaries
- Amendment accepted/rejected/superseded behavior
- No mutation of an accepted term version
- Closed/cancelled/disputed transition rules

### Database integrity and concurrency tests

- Two simultaneous acceptances produce one acceptance
- Two simultaneous payment confirmations produce one financial effect
- Same idempotency key returns the original receipt
- Different payload with a reused idempotency key is rejected
- Stale draft `row_version` receives a conflict
- A failed command leaves no partial cashflow, event, projection, or outbox row
- Projection rebuild equals live canonical balance
- Reversal restores the mathematically expected balance
- Currency and participant foreign-key violations fail at the database boundary

### Identity and invitation tests

- E.164 phone normalization and case-normalized email
- Existing phone user, existing email user, and first-time recipient
- Invite identity must match a verified account identity
- Forwarded, expired, revoked, already-redeemed, and tampered tokens
- Raw token never appears in the database or application logs
- One account may link invited email and phone through existing identity rules
- Email and phone owned by different accounts never auto-merge
- Inviter cannot enumerate whether a contact already has a Fyn account
- Resend retires or reuses the correct secure invitation state without duplicate participants

### Authorization and privacy tests

- Lender, borrower, unclaimed invitee, unrelated user, and deleted user access
- Neither participant can access the other's private transaction or account details
- Raw shared tables remain unavailable to the generic analyst SQL lane
- Account deletion removes private drafts and detaches acknowledged shared records without destroying the counterparty's copy
- Export contains the requesting participant's accessible shared record and omits secrets/provider credentials
- Hidden/archived status is per participant, not shared deletion

### Notification tests

- Outbox row commits with the domain event
- No provider call occurs before commit
- Retry after provider timeout does not duplicate the domain event
- Provider webhooks are idempotent
- Quiet hours, timezone, opt-out, frequency cap, and snooze
- Disputed, cancelled, or closed loans stop automatic reminders
- SMS omits sensitive amount/details before authentication
- Email links use the correct authenticated origin and do not leak tokens through referrers
- Dead-letter state is visible and safely replayable

### API tests

- Every route requires an authenticated user or a valid invitation exchange state
- Caller-supplied user/participant IDs cannot escape membership scope
- All invalid state transitions return stable typed errors
- Idempotency and stale-version contracts
- Pagination and deterministic ordering for portfolio/activity
- Validation limits for amount, dates, note length, attachments, and reminder text
- Rate limits for invitations, resends, manual reminders, and token exchange

### Frontend component and accessibility tests

- Perspective copy is correct for lender and borrower
- Confirmed, pending, disputed, late, closed, and private states are distinguishable without color alone
- Keyboard-only completion of creation, invitation review, payment confirmation, and closure
- Focus returns correctly after dialogs and errors
- Screen reader labels explain amounts, dates, progress, and verification badges
- Responsive layouts at 320/375/430 px and desktop
- Empty, loading, partial-error, stale-version, offline, and retry states
- No threatening copy in normal reminder paths
- Currency, date, and Indian number formatting
- Revision comparison, authorship, comment, acceptance, and stale-proposal states

### End-to-end journeys

1. Hari signs in by phone, invites Rahul by email, Rahul creates an account, proposes a date change, Hari accepts, and both see the same plan.
2. Hari records prior funding, Rahul acknowledges, Rahul records a partial repayment, Hari confirms, and the balance updates once.
3. The browser retries payment confirmation after a simulated timeout and no duplicate is created.
4. An SMS reminder provider fails, the record remains intact, the worker retries, and delivery status recovers.
5. Rahul requests more time, Hari accepts a revised schedule, and the old accepted schedule remains in history.
6. A bank transaction is suggested as a match, the user confirms it, and the shared payment is not duplicated.
7. The balance reaches zero, both close the loan, and an assurance item is acknowledged as returned.
8. One participant deletes their account; the other retains the minimized acknowledged record and no longer has a Fyn communication path to the deleted account.

### Migration and regression tests

- Existing seeded and user-created `loans` remain queryable after migration
- Existing affordability/loan calculator behavior is unchanged
- Semantic manifest, SQL gate, privacy registry, generated contracts, and architecture invariant tests pass
- Alembic upgrade from the current head and full downgrade/upgrade in an isolated test database

## 16. Definition of done and final demo

The feature is complete only when the following works in two clean browser profiles without database intervention:

1. Hari signs in with a phone number.
2. Hari creates a ₹50,000 zero-interest plan and invites Rahul by email.
3. Rahul verifies that email and suggests a different return date.
4. Hari accepts; both see the same active balance and exact acknowledgement history.
5. Rahul records ₹10,000 paid; Hari confirms it; both show ₹40,000 remaining.
6. Repeating the confirmation request produces no duplicate effect.
7. Hari sends a gentle reminder; Rahul sees it in-app and receives the configured channel message.
8. Rahul requests an extension; Hari accepts; the prior terms remain visible.
9. Both can compare the amendment with the prior revision, see who changed each field, and download both immutable documents.
10. The final repayment is confirmed and both close the record.
11. Any assurance item is marked returned, and both can download the activity summary.
12. The copilot accurately answers what is owed, what is due, and what needs a response, and previews every mutation before execution.
13. Backend tests, frontend tests, typecheck, lint, production build, and Playwright end-to-end tests all pass.

This demo is the implementation goal. Features that do not strengthen this journey, record integrity, participant safety, or recoverability should not delay the first release.
