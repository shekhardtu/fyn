import { Menu } from "@base-ui/react/menu";
import { ArrowUp, BrainCircuit, Check, ChevronDown, File as FileIcon, Loader2, Paperclip, ShieldCheck, Sparkles, Square, X, Zap } from "lucide-react";
import { type FormEvent, type RefObject, useId, useMemo } from "react";
import { ComposerTransaction } from "@/components/composer-transaction";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useUserDefaults } from "@/components/user-defaults";
import { EFFORT_OPTIONS, type ComposerEffort } from "@/lib/composer";
import { formatBytes, formatMoney, readComposerEntry } from "@/lib/format";
import type { AttachmentDraft } from "@/components/use-conversation-attachments";
import { attachmentReadingLabel, AttachmentCard } from "@/components/attachment-card";
import { cn } from "@/lib/utils";
import { contractLimits } from "@/lib/generated/contracts";

export function ConversationComposer({ variant, value, onValueChange, onSubmit, onStop, textRef, fileRef, onAttach, attachments, onRemoveAttachment, onRetryAttachment, onImportAttachment, effort, onEffortChange, busy, sending, running, stopping, paused, disabled, dragging }: {
  variant: "focused" | "docked";
  value: string;
  onValueChange: (value: string) => void;
  onSubmit: (event?: FormEvent) => void;
  onStop: () => void;
  textRef: RefObject<HTMLTextAreaElement | null>;
  fileRef: RefObject<HTMLInputElement | null>;
  onAttach: (files: File[]) => void;
  attachments: AttachmentDraft[];
  onRemoveAttachment: (id: string) => void;
  onRetryAttachment: (id: string) => void;
  onImportAttachment?: (file: NonNullable<AttachmentDraft["attachment"]>) => void;
  effort: ComposerEffort;
  onEffortChange: (effort: ComposerEffort) => void;
  busy: boolean;
  sending: boolean;
  running: boolean;
  stopping: boolean;
  paused: boolean;
  disabled: boolean;
  dragging: boolean;
}) {
  const { currency } = useUserDefaults();
  const hintId = useId();
  const reading = useMemo(() => readComposerEntry(value), [value]);
  const attachmentsBlocked = attachments.some((item) => item.status !== "ready");
  const selectedEffort = EFFORT_OPTIONS.find((option) => option.value === effort)!;
  const EffortIcon = effort === "quick" ? Zap : effort === "thorough" ? BrainCircuit : Sparkles;
  const locked = busy || disabled;

  return <form onSubmit={onSubmit} aria-label="Finance composer" className={cn("finance-composer pointer-events-auto mx-auto w-full", variant === "docked" && "max-w-[var(--column-w)]")}>
    <div data-dropping={dragging || undefined} className="entry-card composer-card">
      {dragging ? <div className="composer-drop-cue"><Paperclip size={17} /><span>Drop a file here</span></div> : null}
      {attachments.length ? <div className="composer-files" aria-label="Attachments">{attachments.map((item) => <div key={item.id} className="composer-attachment" aria-label={`Attached file: ${item.filename}`}>
        <span className="composer-file-icon"><FileIcon size={20} /></span>
        <div className="min-w-0 flex-1">{item.attachment && item.status === "ready" ? <AttachmentCard file={item.attachment} compact /> : <p className="truncate text-note font-semibold text-ink" title={item.filename}>{item.filename}</p>}<p className="mt-0.5 text-meta text-ink-muted" role={item.status === "error" ? "alert" : "status"}>{item.status === "uploading" ? item.percent === 100 ? "Preparing file…" : `Uploading ${item.percent}%` : item.error ?? (item.attachment ? attachmentReadingLabel(item.attachment) : "Ready to send")} · {formatBytes(item.byteSize)}</p>
          {item.status === "uploading" ? <div role="progressbar" aria-label={`Uploading ${item.filename}`} aria-valuenow={item.percent} aria-valuemin={0} aria-valuemax={100} className="mt-2 h-1 overflow-hidden rounded-full bg-surface-sunken"><div className="h-full bg-secondary" style={{ width: `${item.percent}%` }} /></div> : null}
          {item.attachment?.media_type === "text/csv" && item.status === "ready" && onImportAttachment ? <button type="button" disabled={locked} className="mt-1 text-meta font-medium text-secondary underline underline-offset-2 disabled:opacity-50" onClick={() => onImportAttachment(item.attachment!)}>Review transactions for import</button> : null}
        </div>
        {item.status === "uploading" ? <Loader2 size={16} className="shrink-0 animate-spin text-secondary" /> : item.status === "error" && !item.attachment ? <Button type="button" variant="ghost" onClick={() => onRetryAttachment(item.id)}>Retry</Button> : null}
        <Button type="button" variant="ghost" size="icon-lg" aria-label={`Remove ${item.filename}`} disabled={locked} onClick={() => onRemoveAttachment(item.id)}><X /></Button>
      </div>)}</div> : null}
      <Textarea ref={textRef} value={value} disabled={disabled || paused} onChange={(event) => onValueChange(event.target.value)} onKeyDown={(event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); if (!locked && !paused && !attachmentsBlocked) onSubmit(); }
      }} onPaste={(event) => {
        if (event.clipboardData.files.length) { event.preventDefault(); onAttach(Array.from(event.clipboardData.files)); }
      }} placeholder={disabled ? "Opening conversation…" : paused ? "Respond to the card above to continue…" : "Ask about your money, or describe a transaction…"} aria-label="Message fyn AI" aria-describedby={hintId} rows={2} className="composer-textarea max-h-36 resize-none border-0 bg-transparent text-control shadow-none placeholder:text-ink-muted" />
      <div className="composer-toolbar">
        <input ref={fileRef} type="file" multiple className="sr-only" tabIndex={-1} aria-hidden aria-label="Choose a file" onChange={(event) => { onAttach(Array.from(event.target.files ?? [])); event.currentTarget.value = ""; }} />
        <Button type="button" variant="ghost" size="icon-lg" disabled={locked || paused || attachments.length >= contractLimits.attachmentsPerMessage} onClick={() => fileRef.current?.click()} className="composer-attach-trigger" aria-label="Add attachment" title="Attach files or images · up to 10 MB each"><Paperclip /></Button>
        <span aria-hidden className="composer-toolbar-divider" />
        <ComposerTransaction disabled={locked || paused} />
        <div className="composer-toolbar-end">
          <Menu.Root>
            <Menu.Trigger render={<Button type="button" variant="ghost" disabled={locked || paused} className="composer-effort-trigger" aria-label={`Thinking effort: ${selectedEffort.label}`} title={selectedEffort.detail} />}><EffortIcon /><span>{selectedEffort.label}</span><ChevronDown className="composer-chevron" /></Menu.Trigger>
            <Menu.Portal><Menu.Positioner side="top" align="end" sideOffset={10} collisionPadding={12} className="z-50"><Menu.Popup className="composer-menu">
              <Menu.Group><Menu.GroupLabel className="composer-menu-heading">Thinking effort</Menu.GroupLabel>
                <Menu.RadioGroup value={effort} onValueChange={(next) => onEffortChange(next as ComposerEffort)}>
                  {EFFORT_OPTIONS.map((option) => {
                    const Icon = option.value === "quick" ? Zap : option.value === "thorough" ? BrainCircuit : Sparkles;
                    return <Menu.RadioItem key={option.value} value={option.value} className="composer-menu-option"><Icon /><span className="min-w-0 flex-1"><span className="composer-option-title">{option.label}{option.value === "auto" ? <span className="composer-file-type">Default</span> : null}</span><span className="composer-option-description">{option.description}</span></span><Menu.RadioItemIndicator><Check size={15} /></Menu.RadioItemIndicator></Menu.RadioItem>;
                  })}
                </Menu.RadioGroup>
              </Menu.Group>
              <p className="composer-menu-footnote">Applies to your next message. Thorough may take longer.</p>
            </Menu.Popup></Menu.Positioner></Menu.Portal>
          </Menu.Root>
          {running
            ? <Button type="button" size="icon-lg" variant="outline" disabled={stopping} onClick={onStop} className="composer-send" aria-label={stopping ? "Stopping fyn AI" : "Stop fyn AI"}>{stopping ? <Loader2 className="animate-spin" /> : <Square size={14} fill="currentColor" />}</Button>
            : <Button type="submit" size="icon-lg" disabled={(!value.trim() && !attachments.length) || locked || paused || attachmentsBlocked} className="composer-send" aria-label="Send message" title="Send message">{sending ? <Loader2 className="animate-spin" /> : <ArrowUp />}</Button>}
        </div>
      </div>
    </div>
    <div id={hintId} className="composer-footer">
      <p className="entry-hint" role="status">{paused ? "Respond to the card above to continue." : attachments.length ? <><ShieldCheck size={13} /><span>Files stay in this chat. Attaching alone won’t add transactions.</span></> : reading ? <><span className={cn("money font-semibold", reading.kind === "income" ? "text-money-in" : reading.kind === "expense" ? "text-money-out" : "text-ink-body")}>{formatMoney(reading.amountMinor, currency)}</span><span>{reading.kind} · complete entries save when sent</span></> : <><ShieldCheck size={13} className="shrink-0" /><span>Complete entries save when sent. You can edit them after.</span></>}</p>
      <span className="composer-key-hint"><kbd>↵</kbd> Send <span>·</span> <kbd>⇧ ↵</kbd> New line</span>
    </div>
  </form>;
}
