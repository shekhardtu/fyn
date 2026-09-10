import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { attachmentContentUrl, uploadAttachment } from "@/lib/attachments";
import type { AttachmentOut } from "@/lib/generated/contracts";

const file: AttachmentOut = { id: "46b8e280-2b10-4482-8ecf-932243331dd7", conversation_id: "9f7b7fa1-3e81-4a8d-8cbd-a00b03dd7654", message_id: null, filename: "note.txt", byte_size: 4, media_type: "text/plain", status: "ready", read_mode: "native", read_error: null, content_metadata: {}, created_at: "2026-09-09T00:00:00Z" };
const fetchMock = vi.fn();
const requests: FakeXHR[] = [];
class FakeXHR {
  withCredentials = true;
  timeout = 0;
  status = 200;
  url = "";
  upload: { onprogress?: (event: { lengthComputable: boolean; loaded: number; total: number }) => void } = {};
  headers: Record<string, string> = {};
  onload?: () => void;
  onloadend?: () => void;
  open(method: string, url: string) { expect(method).toBe("PUT"); this.url = url; requests.push(this); }
  setRequestHeader(name: string, value: string) { this.headers[name] = value; }
  send(body: File) { expect(body.name).toBe("note.txt"); queueMicrotask(() => { this.upload.onprogress?.({ lengthComputable: true, loaded: 4, total: 4 }); this.onload?.(); this.onloadend?.(); }); }
  abort() {}
}

beforeEach(() => {
  requests.length = 0;
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
});
afterEach(() => vi.unstubAllGlobals());

describe("attachment transport", () => {
  it("uploads bytes to R2 without credentials and returns only server-completed metadata", async () => {
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({ attachment: { ...file, status: "uploading" }, upload_url: "https://private-r2.example/upload", headers: { "Content-Type": "application/octet-stream" }, expires_in: 300 }) });
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => file });
    const progress = vi.fn();
    expect(await uploadAttachment(file.conversation_id, new File(["note"], "note.txt"), progress, new AbortController().signal)).toEqual(file);
    expect(requests[0].withCredentials).toBe(false);
    expect(requests[0].headers).toEqual({ "Content-Type": "application/octet-stream" });
    expect(fetchMock.mock.calls.every(([, init]) => init.credentials === "include")).toBe(true);
    expect(fetchMock.mock.calls[1][0]).toContain(`/${file.id}/complete`);
    expect(progress).toHaveBeenCalledWith(100);
  });

  it("removes a draft when completion fails instead of reporting an attachment ready", async () => {
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({ attachment: { ...file, status: "uploading" }, upload_url: "https://private-r2.example/upload", headers: {}, expires_in: 300 }) });
    fetchMock.mockResolvedValueOnce({ ok: false, json: async () => ({ detail: "Invalid file" }) });
    fetchMock.mockResolvedValueOnce({ ok: true, status: 204 });
    await expect(uploadAttachment(file.conversation_id, new File(["note"], "note.txt"), vi.fn(), new AbortController().signal)).rejects.toThrow("Invalid file");
    expect(fetchMock.mock.calls[2][1].method).toBe("DELETE");
  });

  it("rejects size errors before creating remote objects", async () => {
    await expect(uploadAttachment(file.conversation_id, new File([], "note.txt"), vi.fn(), new AbortController().signal)).rejects.toThrow("non-empty");
    const oversized = new File(["note"], "note.txt");
    Object.defineProperty(oversized, "size", { value: 10 * 1024 * 1024 + 1 });
    await expect(uploadAttachment(file.conversation_id, oversized, vi.fn(), new AbortController().signal)).rejects.toThrow("10 MB");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("keeps saved links on the authenticated API rather than persisting signed R2 URLs", () => {
    expect(attachmentContentUrl(file)).toContain(`/conversations/${file.conversation_id}/attachments/${file.id}/content`);
    expect(attachmentContentUrl(file, true)).toContain("?inline=true");
  });
});
