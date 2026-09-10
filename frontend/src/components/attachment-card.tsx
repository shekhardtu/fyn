import { Dialog } from "@base-ui/react/dialog";
import { Download, ExternalLink, File, Paperclip, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { attachmentContentUrl } from "@/lib/attachments";
import { formatBytes } from "@/lib/format";
import type { AttachmentOut } from "@/lib/generated/contracts";

export function attachmentReadingLabel(file: AttachmentOut) {
  if (file.read_mode === "unavailable") return file.read_error ?? "Saved · reading unavailable";
  return "Available to fyn in this conversation";
}

export function AttachmentCard({ file, compact = false }: { file: AttachmentOut; compact?: boolean }) {
  const isImage = file.media_type.startsWith("image/");
  return <Dialog.Root>
    <Dialog.Trigger render={<button type="button" className={compact ? "flex max-w-full items-center gap-2 text-left text-secondary hover:underline" : "attachment-card"} aria-label={`View ${file.filename}`} />}>
      {compact ? null : isImage ? <img src={attachmentContentUrl(file, true)} alt="" loading="lazy" className="size-12 shrink-0 rounded-lg object-cover" /> : <span className="grid size-12 shrink-0 place-items-center rounded-lg bg-surface-sunken text-ink-muted"><File size={23} /></span>}
      <span className="min-w-0 flex-1 text-left"><span className="block truncate text-note font-medium text-ink">{file.filename}</span>{!compact ? <span className="mt-1 block text-meta text-ink-muted">{formatBytes(file.byte_size)} · {file.read_mode === "unavailable" ? "Reading unavailable" : "Saved attachment"}</span> : null}</span>
    </Dialog.Trigger>
    <Dialog.Portal>
      <Dialog.Backdrop className="fixed inset-0 z-50 bg-scrim/40 backdrop-blur-[2px]" />
      <Dialog.Popup className="fixed inset-x-3 top-1/2 z-50 mx-auto flex max-h-[85dvh] max-w-3xl -translate-y-1/2 flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-[var(--shadow-overlay)] outline-none">
        <div className="flex items-center gap-3 border-b border-line px-4 py-3"><div className="min-w-0 flex-1"><Dialog.Title className="truncate font-semibold text-ink">{file.filename}</Dialog.Title><Dialog.Description className="mt-1 text-meta text-ink-muted">{attachmentReadingLabel(file)}</Dialog.Description></div><Dialog.Close render={<Button type="button" variant="ghost" size="icon-lg" aria-label="Close attachment" />}><X /></Dialog.Close></div>
        <div className="min-h-0 overflow-auto p-4">{isImage ? <img src={attachmentContentUrl(file, true)} alt={file.filename} className="mx-auto max-h-[60dvh] max-w-full rounded-lg object-contain" /> : <div className="flex flex-col items-center gap-3 rounded-xl bg-surface-sunken px-4 py-10 text-center"><File size={40} className="text-ink-muted" /><p className="text-note text-ink-muted">{formatBytes(file.byte_size)} · {file.media_type === "application/pdf" ? `${file.content_metadata.pageCount ?? ""} pages` : "Original file"}</p><p className="max-w-md text-note text-ink-muted">{attachmentReadingLabel(file)}</p></div>}</div>
        <div className="flex flex-wrap justify-end gap-3 border-t border-line px-4 py-3">
          {file.media_type === "application/pdf" ? <a href={attachmentContentUrl(file, true)} target="_blank" rel="noreferrer" className="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-note text-secondary"><ExternalLink size={16} />Open PDF</a> : null}
          <a href={attachmentContentUrl(file)} target="_blank" rel="noreferrer" className="inline-flex items-center gap-2 rounded-lg bg-secondary px-3 py-2 text-note text-on-secondary"><Download size={16} />Download original</a>
        </div>
      </Dialog.Popup>
    </Dialog.Portal>
  </Dialog.Root>;
}

export function ConversationFiles({ files, loading = false, error, onRetry }: {
  files: AttachmentOut[];
  loading?: boolean;
  error?: Error | null;
  onRetry?: () => void;
}) {
  const sent = files.filter((file) => file.message_id);
  return <Dialog.Root>
    <Dialog.Trigger render={<Button type="button" variant="ghost" size="icon-lg" aria-label={`Conversation files${sent.length ? ` (${sent.length})` : ""}`} className="relative rounded-xl text-ink-muted" />}><Paperclip />{sent.length ? <span className="absolute top-0 right-0 grid min-w-4 place-items-center rounded-full bg-secondary px-1 text-[10px] text-on-secondary">{sent.length}</span> : null}</Dialog.Trigger>
    <Dialog.Portal><Dialog.Backdrop className="fixed inset-0 z-40 bg-scrim/30" /><Dialog.Popup className="fixed inset-y-3 right-3 left-3 z-40 ml-auto flex max-w-md flex-col rounded-2xl border border-line bg-surface shadow-[var(--shadow-overlay)] outline-none">
      <div className="flex items-start gap-3 border-b border-line px-5 py-4"><div className="flex-1"><Dialog.Title className="font-heading text-title font-semibold">Conversation files</Dialog.Title><Dialog.Description className="mt-1 text-note text-ink-muted">Files you’ve sent here stay available for follow-up questions.</Dialog.Description></div><Dialog.Close render={<Button type="button" variant="ghost" size="icon-lg" aria-label="Close conversation files" />}><X /></Dialog.Close></div>
      <div className="flex-1 space-y-3 overflow-auto p-4">
        {error ? <div role="alert" className="space-y-3 px-1 py-3 text-note text-ink-muted"><p>Couldn’t refresh the conversation’s files.</p><Button variant="outline" onClick={onRetry}>Try again</Button></div> : null}
        {sent.map((file) => <AttachmentCard key={file.id} file={file} />)}
        {!sent.length && !error ? <p role={loading ? "status" : undefined} className="px-1 py-6 text-note text-ink-muted">{loading ? "Loading conversation files…" : "No files sent yet. Use the paperclip in the composer to attach a file or image."}</p> : null}
      </div>
    </Dialog.Popup></Dialog.Portal>
  </Dialog.Root>;
}
