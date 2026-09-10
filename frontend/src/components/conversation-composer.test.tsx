import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createRef, useState, type ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConversationComposer } from "@/components/conversation-composer";
import type { ComposerEffort } from "@/lib/composer";
import * as api from "@/lib/api";

type Props = ComponentProps<typeof ConversationComposer>;
function setup(overrides: Partial<Props> = {}) {
  const onSubmit = vi.fn((event?: { preventDefault: () => void }) => event?.preventDefault());
  const onStop = vi.fn();
  const onAttach = vi.fn();
  const onRemoveAttachment = vi.fn();
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } });
  function Harness() {
    const [value, setValue] = useState(overrides.value ?? "");
    const [effort, setEffort] = useState<ComposerEffort>("auto");
    return <QueryClientProvider client={client}><ConversationComposer variant="docked" textRef={createRef()} fileRef={createRef()} onSubmit={onSubmit} onStop={onStop} onAttach={onAttach} attachments={[]} onRetryAttachment={vi.fn()} onRemoveAttachment={onRemoveAttachment} busy={false} sending={false} running={false} stopping={false} paused={false} disabled={false} dragging={false} {...overrides} value={value} onValueChange={setValue} effort={effort} onEffortChange={setEffort} /></QueryClientProvider>;
  }
  render(<Harness />);
  return { onSubmit, onStop, onAttach, onRemoveAttachment, client };
}

afterEach(() => vi.restoreAllMocks());

describe("finance composer journey", () => {
  it("keeps the draft editable and allows cancellation while upload blocks sending", () => {
    const { onRemoveAttachment, onSubmit } = setup({ value: "Read this", attachments: [{ id: "file-1", filename: "receipt.png", byteSize: 1024, status: "uploading", percent: 42 }] });
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    expect(screen.getByRole("textbox")).toBeEnabled();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "42");
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Remove receipt.png" }));
    expect(onRemoveAttachment).toHaveBeenCalledWith("file-1");
  });
  it("keeps input and actions separate and disables empty sends", () => {
    setup();
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add transaction" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Thinking effort: Auto" })).toBeEnabled();
  });

  it("opens a generic file picker directly without categorizing or sending", () => {
    const { onSubmit, onAttach } = setup({ value: "Keep this draft" });
    const fileInput = screen.getByLabelText("Choose a file");
    const openPicker = vi.spyOn(fileInput, "click");
    fireEvent.click(screen.getByRole("button", { name: "Add attachment" }));
    expect(openPicker).toHaveBeenCalledOnce();
    expect(fileInput).not.toHaveAttribute("accept");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue("Keep this draft");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onAttach).not.toHaveBeenCalled();
  });

  it("passes the selected file to validation without requiring a category", () => {
    const { onAttach, onSubmit } = setup();
    const file = new File(["image"], "receipt.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Choose a file"), { target: { files: [file] } });
    expect(onAttach).toHaveBeenCalledWith([file]);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("changes effort without changing or sending the draft", () => {
    const { onSubmit } = setup({ value: "Compare my spending" });
    fireEvent.click(screen.getByRole("button", { name: "Thinking effort: Auto" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: /Thorough/ }));
    expect(screen.getByRole("button", { name: "Thinking effort: Thorough" })).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue("Compare my spending");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("keeps a saved file removable and sends it with the chosen effort", () => {
    const { onSubmit, onRemoveAttachment } = setup({ attachments: [{ id: "file-1", filename: "statement.csv", byteSize: 42, status: "ready", percent: 100 }] });
    expect(screen.getByText(/Attaching alone won’t add transactions/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Thinking effort: Auto" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Remove statement.csv" }));
    expect(onRemoveAttachment).toHaveBeenCalledOnce();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("does not submit Enter during IME composition or Shift+Enter", () => {
    const { onSubmit } = setup({ value: "Lunch" });
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter", shiftKey: true });
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter", isComposing: true });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledOnce();
  });

  it("keeps Stop available while disabling intake during a run", () => {
    const { onStop, onSubmit } = setup({ busy: true, running: true, value: "Next question" });
    expect(screen.getByRole("button", { name: "Add attachment" })).toBeDisabled();
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Stop fyn AI" }));
    expect(onStop).toHaveBeenCalledOnce();
  });

  it("returns from quick entry with both drafts intact", async () => {
    const { onSubmit } = setup({ value: "My question is still here" });
    fireEvent.click(screen.getByRole("button", { name: "Add transaction" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Transaction amount" }), { target: { value: "540" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Merchant" }), { target: { value: "Cafe" } });
    fireEvent.click(screen.getByRole("button", { name: "Back to chat" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByRole("textbox", { name: "Message fyn AI" })).toHaveValue("My question is still here");
    fireEvent.click(screen.getByRole("button", { name: "Add transaction" }));
    expect(screen.getByRole("textbox", { name: "Transaction amount" })).toHaveValue("540");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("saves a transaction once without submitting chat and refreshes totals", async () => {
    const save = vi.spyOn(api, "createTransactionRecord").mockResolvedValue({ amountMinor: 54_000, currency: "INR", transactionType: "expense" } as Awaited<ReturnType<typeof api.createTransactionRecord>>);
    const { onSubmit, client } = setup({ value: "Keep my question" });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    fireEvent.click(screen.getByRole("button", { name: "Add transaction" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Transaction amount" }), { target: { value: "540" } });
    fireEvent.click(screen.getByRole("button", { name: "Save transaction" }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(save).toHaveBeenCalledWith(expect.objectContaining({ amountMinor: 54_000, expectedVersion: null }), expect.anything());
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["overview"] });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: "Message fyn AI" })).toHaveValue("Keep my question");
  });

  it("keeps entered details available when saving fails", async () => {
    vi.spyOn(api, "createTransactionRecord").mockRejectedValue(new Error("Connection lost. Try again."));
    setup();
    fireEvent.click(screen.getByRole("button", { name: "Add transaction" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Transaction amount" }), { target: { value: "540" } });
    fireEvent.click(screen.getByRole("button", { name: "Save transaction" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Connection lost");
    expect(screen.getByRole("textbox", { name: "Transaction amount" })).toHaveValue("540");
    expect(screen.getByRole("button", { name: "Save transaction" })).toBeEnabled();
  });
});
