import { conversationFile, deleteFile, fileContentUrl, listFiles, previewFileImport, uploadFile } from "@/lib/api";
import type { AttachmentOut } from "@/lib/generated/contracts";

export const attachmentQueryKey = (threadId: string) => ["attachments", threadId] as const;

export async function listAttachments(threadId: string): Promise<AttachmentOut[]> {
  return (await listFiles("conversation", threadId)).map(conversationFile);
}

export async function removeAttachment(_threadId: string, id: string): Promise<void> {
  await deleteFile(id);
}

export function previewAttachmentImport(file: AttachmentOut) {
  return previewFileImport(file.conversation_id, file.id);
}

export function attachmentContentUrl(file: AttachmentOut, inline = false): string {
  return fileContentUrl(file.id, inline);
}

export async function uploadAttachment(threadId: string, file: File, progress: (percent: number) => void, signal: AbortSignal, onStaged?: (attachment: AttachmentOut) => void): Promise<AttachmentOut> {
  return conversationFile(await uploadFile(file, { purpose: "conversation", conversation_id: threadId }, progress, signal,
    (reserved) => onStaged?.(conversationFile(reserved))));
}
