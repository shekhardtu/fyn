import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { useConversationAttachments } from "@/components/use-conversation-attachments";
import { attachmentQueryKey, listAttachments, removeAttachment, uploadAttachment } from "@/lib/attachments";
import type { AttachmentOut } from "@/lib/generated/contracts";

vi.mock("@/lib/attachments", () => ({
  attachmentQueryKey: (id: string) => ["attachments", id],
  listAttachments: vi.fn(), removeAttachment: vi.fn(), uploadAttachment: vi.fn(),
}));
const saved: AttachmentOut = { id: "file-1", conversation_id: "thread-1", message_id: null, filename: "notes.txt", byte_size: 4, media_type: "text/plain", status: "ready", read_mode: "native", read_error: null, content_metadata: {}, created_at: "2026-09-09T00:00:00Z" };

function setup(initial = "thread-1") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return { client, ...renderHook(({ id }) => useConversationAttachments(id), { wrapper, initialProps: { id: initial } }) };
}

beforeEach(() => {
  vi.mocked(listAttachments).mockReset().mockResolvedValue([]);
  vi.mocked(removeAttachment).mockReset().mockResolvedValue(undefined);
  vi.mocked(uploadAttachment).mockReset();
});

it("restores saved drafts and marks sent files without deleting originals", async () => {
  vi.mocked(listAttachments).mockResolvedValue([saved]);
  const { result, client } = setup();
  await waitFor(() => expect(result.current.ready).toHaveLength(1));
  act(() => result.current.sent([saved.id], "message-1"));
  await waitFor(() => expect(result.current.drafts).toHaveLength(0));
  expect(client.getQueryData<AttachmentOut[]>(attachmentQueryKey("thread-1"))?.[0].message_id).toBe("message-1");
  expect(removeAttachment).not.toHaveBeenCalled();
});

it("enforces the file count across rapid selections before a rerender", async () => {
  vi.mocked(uploadAttachment).mockReturnValue(new Promise(() => undefined));
  const { result } = setup();
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => {
    result.current.add(Array.from({ length: 5 }, (_, index) => new File(["note"], `note-${index}.txt`)));
    expect(() => result.current.add([new File(["extra"], "extra.txt")])).toThrow("five files");
  });
  expect(uploadAttachment).toHaveBeenCalledTimes(5);
});

it("cancels local uploads when switching threads and ignores their late results", async () => {
  let finish: (file: AttachmentOut) => void = () => undefined;
  vi.mocked(uploadAttachment).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const { result, rerender, client } = setup();
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => result.current.add([new File(["note"], "notes.txt")]));
  const signal = vi.mocked(uploadAttachment).mock.calls[0][3];
  rerender({ id: "thread-2" });
  expect(signal.aborted).toBe(true);
  await act(async () => finish(saved));
  expect(result.current.drafts).toHaveLength(0);
  expect(client.getQueryData(attachmentQueryKey("thread-2"))).not.toContainEqual(saved);
});

it("replaces a failed upload without leaving its cached draft behind on retry", async () => {
  vi.mocked(uploadAttachment).mockImplementationOnce(async (_thread, _file, _progress, _signal, staged) => {
    staged?.({ ...saved, status: "uploading" });
    throw new Error("Connection lost");
  }).mockReturnValue(new Promise(() => undefined));
  const { result, client } = setup();
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => result.current.add([new File(["note"], "notes.txt")]));
  await waitFor(() => expect(result.current.drafts[0].status).toBe("error"));
  act(() => client.setQueryData(attachmentQueryKey("thread-1"), [{ ...saved, status: "uploading" }]));
  act(() => result.current.retry(result.current.drafts[0].id));
  expect(result.current.drafts).toHaveLength(1);
  expect(result.current.drafts[0].status).toBe("uploading");
  expect(client.getQueryData(attachmentQueryKey("thread-1"))).toEqual([]);
});
