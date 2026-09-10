import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { attachmentContentUrl, uploadAttachment } from "@/lib/attachments";
import { ApiError, documentAssetDownloadUrl, uploadDocumentAsset, uploadFile } from "@/lib/api";
import type { AttachmentOut, FileOut } from "@/lib/generated/contracts";

const attachment: AttachmentOut = { id: "46b8e280-2b10-4482-8ecf-932243331dd7", conversation_id: "9f7b7fa1-3e81-4a8d-8cbd-a00b03dd7654", message_id: null, filename: "note.txt", byte_size: 4, media_type: "text/plain", status: "ready", read_mode: "native", read_error: null, content_metadata: {}, created_at: "2026-09-09T00:00:00Z" };
const file: FileOut = { ...attachment, purpose: "conversation", sha256: "a".repeat(64), classification: null, description: null, document_state: null };
const fetchMock = vi.fn();
const requests: FakeXHR[] = [];
let response: unknown;
let responseStatus: number;
let pending: boolean;
class FakeXHR {
  withCredentials = false;
  timeout = 0;
  status = 200;
  responseText = "";
  url = "";
  upload: { onprogress?: (event: { lengthComputable: boolean; loaded: number; total: number }) => void } = {};
  headers: Record<string, string> = {};
  onload?: () => void;
  onloadend?: () => void;
  onabort?: () => void;
  ontimeout?: () => void;
  open(method: string, url: string) { expect(method).toBe("POST"); this.url = url; requests.push(this); }
  setRequestHeader(name: string, value: string) { this.headers[name] = value; }
  send(body: File) {
    expect(body).toBeInstanceOf(File);
    if (pending) return;
    queueMicrotask(() => {
      this.status = responseStatus;
      this.responseText = JSON.stringify(response);
      this.upload.onprogress?.({ lengthComputable: true, loaded: body.size, total: body.size });
      this.onload?.(); this.onloadend?.();
    });
  }
  abort() { this.onabort?.(); this.onloadend?.(); }
}
function reserve(saved: FileOut = file) {
  fetchMock.mockResolvedValueOnce({ ok: true, status: 201, json: async () => ({ ...saved, status: "uploading" }) });
}

beforeEach(() => {
  requests.length = 0;
  response = file; responseStatus = 200; pending = false;
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
});
afterEach(() => vi.unstubAllGlobals());

describe("shared file transport", () => {
  it("uses authenticated /files requests and waits for validation before 100%", async () => {
    reserve();
    const progress = vi.fn();
    const staged = vi.fn();
    expect(await uploadAttachment(file.conversation_id!, new File(["note"], "note.txt"), progress, new AbortController().signal, staged)).toMatchObject(attachment);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/files$/);
    expect(fetchMock.mock.calls[0][1].credentials).toBe("include");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toMatchObject({ purpose: "conversation", byte_size: 4, conversation_id: attachment.conversation_id });
    expect(staged).toHaveBeenCalledWith(expect.objectContaining({ id: file.id, status: "uploading" }));
    expect(requests[0].url).toMatch(new RegExp(`/files/${file.id}/content$`));
    expect(requests[0].withCredentials).toBe(true);
    expect(requests[0].headers).toEqual({ "Content-Type": "application/octet-stream" });
    expect(progress.mock.calls.map(([value]) => value)).toEqual([99, 100]);
  });

  it("uses the identical file lifecycle for lending", async () => {
    const document: FileOut = { ...file, purpose: "document", filename: "note.pdf", media_type: "application/pdf", conversation_id: null, classification: "supporting_evidence", document_state: "clean" };
    reserve(document); response = document;
    const saved = await uploadDocumentAsset(new File(["note"], "note.pdf"), "supporting_evidence");
    expect(saved).toMatchObject({ id: file.id, originalFilename: "note.pdf", state: "clean" });
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/files$/);
    expect(requests[0].url).toContain(`/files/${file.id}/content`);
    expect(documentAssetDownloadUrl(saved.id)).toBe(attachmentContentUrl(attachment));
  });

  it("retains authentication errors and deletes failed reservations", async () => {
    reserve(); responseStatus = 401; response = { detail: "Sign in again" };
    fetchMock.mockResolvedValueOnce({ ok: true, status: 204 });
    await expect(uploadAttachment(attachment.conversation_id, new File(["note"], "note.txt"), vi.fn(), new AbortController().signal)).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock.mock.calls[1][0]).toContain(`/files/${file.id}`);
    expect(fetchMock.mock.calls[1][1].method).toBe("DELETE");
  });

  it("cancels the upload and cleans up its reserved identity", async () => {
    reserve(); pending = true;
    fetchMock.mockResolvedValueOnce({ ok: true, status: 204 });
    const controller = new AbortController();
    const promise = uploadFile(new File(["note"], "note.txt"), { purpose: "document" }, undefined, controller.signal);
    const rejected = expect(promise).rejects.toMatchObject({ name: "AbortError" });
    await vi.waitFor(() => expect(requests).toHaveLength(1));
    controller.abort();
    await rejected;
    expect(fetchMock.mock.calls[1][1].method).toBe("DELETE");
  });

  it("queues a five-file selection with at most two active uploads", async () => {
    pending = true;
    for (let index = 0; index < 5; index += 1) reserve();
    const uploads = Array.from({ length: 5 }, () => uploadFile(new File(["note"], "note.txt"), { purpose: "conversation", conversation_id: attachment.conversation_id }));
    await vi.waitFor(() => expect(requests).toHaveLength(2));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    for (let index = 0; index < 5; index += 1) {
      const xhr = requests[index];
      xhr.responseText = JSON.stringify(file);
      xhr.onload?.(); xhr.onloadend?.();
      if (index < 3) await vi.waitFor(() => expect(requests).toHaveLength(index + 3));
    }
    await expect(Promise.all(uploads)).resolves.toHaveLength(5);
  });

  it("rejects empty and oversized files before reserving quota", async () => {
    await expect(uploadFile(new File([], "note.txt"), { purpose: "document" })).rejects.toThrow("non-empty");
    const oversized = new File(["note"], "note.txt");
    Object.defineProperty(oversized, "size", { value: 10 * 1024 * 1024 + 1 });
    await expect(uploadFile(oversized, { purpose: "document" })).rejects.toThrow("10 MB");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("uses one authenticated content URL for previews and downloads", () => {
    expect(attachmentContentUrl(attachment)).toMatch(new RegExp(`/files/${file.id}/content$`));
    expect(attachmentContentUrl(attachment, true)).toContain("?inline=true");
  });
});
