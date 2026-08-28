import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { UserMessage } from "@/components/user-message";

const deliveredAt = "2026-08-29T10:51:31.799Z";
const messageId = "613d433c-f804-4328-a429-e0ac3357f400";

describe("user message", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps the bubble as selectable text rather than an interactive control", () => {
    render(<UserMessage content="Add 500 to food → dinner" messageId={messageId} deliveredAt={deliveredAt} />);

    const content = screen.getByText("Add 500 to food → dinner");
    expect(content.tagName).toBe("P");
    expect(content).toHaveClass("select-text", "cursor-text");
    expect(content.closest("button")).toBeNull();

    fireEvent.click(content);
    expect(screen.queryByText("Message information and actions")).not.toBeInTheDocument();
  });

  it("copies the complete message from its contextual menu and confirms in place", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    render(<UserMessage content="Add 500 to food → dinner" messageId={messageId} deliveredAt={deliveredAt} />);

    fireEvent.click(screen.getByRole("button", { name: "Message options" }));
    fireEvent.click(screen.getByRole("button", { name: "Copy message" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Message copied" })).toHaveTextContent("Copied"));
    expect(writeText).toHaveBeenCalledWith("Add 500 to food → dinner");
  });

  it("reports a clipboard failure instead of showing a false success", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    render(<UserMessage content="Add 500 to food → dinner" messageId={messageId} deliveredAt={deliveredAt} />);

    fireEvent.click(screen.getByRole("button", { name: "Message options" }));
    fireEvent.click(screen.getByRole("button", { name: "Copy message" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Message could not be copied" })).toHaveTextContent("Couldn’t copy"));
  });

  it("groups muted delivery information above the available actions", () => {
    render(<UserMessage content="Add 500 to food → dinner" messageId={messageId} deliveredAt={deliveredAt} />);
    const bubble = screen.getByText("Add 500 to food → dinner").closest(".user-message");

    expect(screen.queryByLabelText("Message information")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Message options" }));

    // The popup is portalled to the overlay layer, not inserted into the
    // virtualised transcript row where it could change the row's measurement.
    expect(bubble).not.toContainElement(screen.getByRole("dialog"));
    expect(screen.getByLabelText("Message information")).toHaveTextContent("Delivery");
    expect(screen.getByLabelText(/Delivered .*local time/)).toBeInTheDocument();
    expect(screen.getByLabelText("Message information")).toHaveTextContent(`Message ID${messageId.slice(0, 8)}`);
    expect(screen.getByRole("button", { name: "Copy message" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy message ID" })).toBeInTheDocument();
  });

  it("shows honest sending status before delivery completes", () => {
    render(<UserMessage content="Add 500 to food → dinner" messageId="optimistic-123" deliveredAt="" />);

    fireEvent.click(screen.getByRole("button", { name: "Message options" }));

    expect(screen.getByText("Sending…")).toBeInTheDocument();
    expect(screen.getByText("Pending")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy message ID" })).not.toBeInTheDocument();
  });
});
