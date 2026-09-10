import { apiUrl } from "@/lib/api";
import { z } from "zod";
import type { AttachmentOut, AttachmentUploadOut } from "@/lib/generated/contracts";
import { contractLimits } from "@/lib/generated/contracts";
import { schemas } from "@/lib/generated/contracts.zod";
import { importResultSchema } from "@/lib/protocol";

export const attachmentQueryKey = (threadId: string) => ["attachments", threadId] as const;
const path = (threadId: string) => `/conversations/${encodeURIComponent(threadId)}/attachments`;

async function control(resource: string, init?: RequestInit) {
  const response = await fetch(apiUrl(resource), {
    credentials: "include", ...init, headers: { "Content-Type": "application/json", ...init?.headers },
    signal: init?.signal ?? AbortSignal.timeout(60_000),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(typeof body?.detail === "string" ? body.detail : "The attachment couldn’t be saved. Try again.");
  }
  return response.status === 204 ? null : response.json();
}

export async function listAttachments(threadId: string): Promise<AttachmentOut[]> {
  return schemas.AttachmentOut.array().parse(await control(path(threadId)));
}

export async function removeAttachment(threadId: string, id: string): Promise<void> {
  await control(`${path(threadId)}/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function previewAttachmentImport(file: AttachmentOut) {
  return importResultSchema.parse(await control(`${path(file.conversation_id)}/${file.id}/import`, { method: "POST" }));
}

export function attachmentContentUrl(file: AttachmentOut, inline = false): string {
  return apiUrl(`${path(file.conversation_id)}/${encodeURIComponent(file.id)}/content${inline ? "?inline=true" : ""}`);
}

function putFile(upload: AttachmentUploadOut, file: File, progress: (percent: number) => void, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abort = () => { xhr.abort(); reject(new DOMException("Upload cancelled", "AbortError")); };
    if (signal.aborted) { abort(); return; }
    xhr.open("PUT", upload.upload_url);
    // This URL is a short-lived capability for one staging object. Application
    // cookies and authorization headers must never be forwarded to R2.
    xhr.withCredentials = false;
    xhr.timeout = 120_000;
    for (const [name, value] of Object.entries(upload.headers)) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) progress(Math.round(event.loaded * 100 / event.total)); };
    xhr.onload = () => xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error("The file upload failed. Try again."));
    xhr.onerror = () => reject(new Error("Couldn’t reach attachment storage. Check your connection and try again."));
    xhr.ontimeout = () => reject(new Error("The upload timed out. Try again."));
    xhr.onloadend = () => signal.removeEventListener("abort", abort);
    signal.addEventListener("abort", abort, { once: true });
    xhr.send(file);
  });
}

export async function uploadAttachment(threadId: string, file: File, progress: (percent: number) => void, signal: AbortSignal, onStaged?: (attachment: AttachmentOut) => void): Promise<AttachmentOut> {
  if (!file.size || file.size > contractLimits.attachmentUploadBytes) throw new Error("Choose a non-empty file up to 10 MB.");
  const upload = schemas.AttachmentUploadOut.extend({ headers: z.record(z.string(), z.string()) }).parse(await control(path(threadId), {
    method: "POST", body: JSON.stringify({ filename: file.name, byte_size: file.size }), signal,
  }));
  onStaged?.(upload.attachment);
  try {
    await putFile(upload, file, progress, signal);
    return schemas.AttachmentOut.parse(await control(`${path(threadId)}/${upload.attachment.id}/complete`, { method: "POST", signal }));
  } catch (error) {
    // A failed/cancelled upload cannot leave an invisible draft using up quota.
    await removeAttachment(threadId, upload.attachment.id).catch(() => undefined);
    throw error;
  }
}
