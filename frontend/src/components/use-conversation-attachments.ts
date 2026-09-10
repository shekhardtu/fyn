import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { attachmentQueryKey, listAttachments, removeAttachment, uploadAttachment } from "@/lib/attachments";
import type { AttachmentOut } from "@/lib/generated/contracts";
import { contractLimits } from "@/lib/generated/contracts";

export type AttachmentDraft = {
  id: string;
  filename: string;
  byteSize: number;
  status: "uploading" | "ready" | "error";
  percent: number;
  attachment?: AttachmentOut;
  error?: string;
};
type LocalUpload = AttachmentDraft & { file: File; controller: AbortController; threadId: string; remoteId?: string };

export function useConversationAttachments(threadId: string) {
  const client = useQueryClient();
  const query = useQuery({ queryKey: attachmentQueryKey(threadId), queryFn: () => listAttachments(threadId), staleTime: 30_000 });
  const [local, setLocal] = useState<LocalUpload[]>([]);
  const [localThread, setLocalThread] = useState(threadId);
  if (localThread !== threadId) { setLocalThread(threadId); setLocal([]); }
  const active = useRef(new Map<string, LocalUpload>());
  const generation = useRef(0);
  useEffect(() => () => {
    generation.current += 1;
    for (const upload of active.current.values()) upload.controller.abort();
    active.current.clear();
  }, [threadId]);

  const update = useCallback((id: string, patch: Partial<AttachmentDraft>) => {
    setLocal((current) => current.map((item) => item.id === id ? { ...item, ...patch } : item));
  }, []);

  const start = useCallback((file: File) => {
    const id = crypto.randomUUID();
    const upload: LocalUpload = { id, file, filename: file.name, byteSize: file.size, status: "uploading", percent: 0, controller: new AbortController(), threadId };
    const version = generation.current;
    active.current.set(id, upload);
    setLocal((current) => [...current, upload]);
    void uploadAttachment(threadId, file, (percent) => update(id, { percent }), upload.controller.signal, (attachment) => {
      upload.remoteId = attachment.id;
      if (version !== generation.current || upload.controller.signal.aborted) return;
      setLocal((current) => current.map((item) => item.id === id ? { ...item, remoteId: attachment.id } : item));
    }).then((attachment) => {
      if (version !== generation.current || upload.controller.signal.aborted) return;
      client.setQueryData<AttachmentOut[]>(attachmentQueryKey(threadId), (current = []) => [...current.filter((item) => item.id !== attachment.id), attachment]);
      setLocal((current) => current.filter((item) => item.id !== id));
      active.current.delete(id);
    }).catch((error: Error) => {
      if (version === generation.current && error.name !== "AbortError") update(id, { status: "error", error: error.message });
    });
  }, [client, threadId, update]);

  const currentLocal = local.filter((item) => item.threadId === threadId);
  const staged = (query.data ?? []).filter((item) => !item.message_id && !currentLocal.some((upload) => upload.remoteId === item.id));
  const drafts: AttachmentDraft[] = [...staged.map((item): AttachmentDraft => ({
    id: item.id, filename: item.filename, byteSize: item.byte_size, attachment: item,
    status: item.status === "ready" ? "ready" : "error", percent: 100,
    ...(item.status === "uploading" ? { error: "Upload interrupted. Remove this file and attach it again." } : {}),
  })), ...currentLocal];

  const add = useCallback((files: File[]) => {
    const pending = [...active.current.values()].filter((item) => item.threadId === threadId);
    const saved = (client.getQueryData<AttachmentOut[]>(attachmentQueryKey(threadId)) ?? []).filter((item) => !item.message_id && !pending.some((upload) => upload.remoteId === item.id));
    if (files.length + saved.length + pending.length > contractLimits.attachmentsPerMessage) throw new Error("Attach up to five files per message.");
    for (const file of files) {
      if (!file.size || file.size > contractLimits.attachmentUploadBytes) throw new Error(`${file.name}: choose a non-empty file up to 10 MB.`);
    }
    files.forEach(start);
  }, [client, threadId, start]);

  const remove = async (id: string) => {
    const upload = active.current.get(id);
    if (upload) {
      upload.controller.abort();
      active.current.delete(id);
      setLocal((current) => current.filter((item) => item.id !== id));
      if (upload.remoteId) client.setQueryData<AttachmentOut[]>(attachmentQueryKey(threadId), (current = []) => current.filter((item) => item.id !== upload.remoteId));
      return;
    }
    await removeAttachment(threadId, id);
    client.setQueryData<AttachmentOut[]>(attachmentQueryKey(threadId), (current = []) => current.filter((item) => item.id !== id));
  };

  const retry = (id: string) => {
    const upload = active.current.get(id);
    if (!upload) return;
    upload.controller.abort();
    active.current.delete(id);
    setLocal((current) => current.filter((item) => item.id !== id));
    if (upload.remoteId) client.setQueryData<AttachmentOut[]>(attachmentQueryKey(threadId), (current = []) => current.filter((item) => item.id !== upload.remoteId));
    start(upload.file);
  };

  const sent = useCallback((ids: string[], messageId: string) => {
    client.setQueryData<AttachmentOut[]>(attachmentQueryKey(threadId), (current = []) => current.map((item) => ids.includes(item.id) ? { ...item, message_id: messageId } : item));
  }, [client, threadId]);
  return { drafts, add, remove, retry, sent, files: query.data ?? [], ready: staged.filter((item) => item.status === "ready"),
    blocked: drafts.some((item) => item.status !== "ready"), loading: query.isPending, error: query.error, refresh: query.refetch };
}
