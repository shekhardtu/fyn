import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MessageIdentifier } from "@/components/message-identifier";

describe("message identifier", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows a compact persisted ID while retaining the complete UUID", () => {
    const messageId = "613d433c-f804-4328-a429-e0ac3357f400";
    render(<MessageIdentifier messageId={messageId} />);

    const identifier = screen.getByRole("button", { name: `Copy Message ID ${messageId}` });
    expect(identifier).toHaveTextContent("ID 613d433c");
    expect(identifier).toHaveAttribute("title", `Message ID ${messageId} — click to copy`);
  });

  it("copies the complete UUID on click and confirms briefly", async () => {
    const messageId = "613d433c-f804-4328-a429-e0ac3357f400";
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    render(<MessageIdentifier messageId={messageId} />);

    fireEvent.click(screen.getByRole("button", { name: `Copy Message ID ${messageId}` }));

    await waitFor(() => expect(screen.getByRole("button")).toHaveTextContent("Copied"));
    expect(writeText).toHaveBeenCalledWith(messageId);
  });

  it("says so when the clipboard refuses, instead of pretending it copied", async () => {
    const messageId = "613d433c-f804-4328-a429-e0ac3357f400";
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    render(<MessageIdentifier messageId={messageId} />);

    fireEvent.click(screen.getByRole("button", { name: `Copy Message ID ${messageId}` }));

    await waitFor(() => expect(screen.getByRole("button")).toHaveTextContent("Couldn’t copy"));
  });

  it("does not present an optimistic browser ID as a posted entry", () => {
    render(<MessageIdentifier messageId="optimistic-123" />);
    expect(screen.getByLabelText("Message ID pending until the message is persisted")).toHaveTextContent("Posting…");
  });
});
