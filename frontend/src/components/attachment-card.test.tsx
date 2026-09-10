import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ConversationFiles } from "@/components/attachment-card";

it("distinguishes an unavailable file list from an empty conversation and offers retry", () => {
  const retry = vi.fn();
  render(<ConversationFiles files={[]} error={new Error("Offline")} onRetry={retry} />);
  fireEvent.click(screen.getByRole("button", { name: "Conversation files" }));
  expect(screen.getByRole("alert")).toHaveTextContent("Couldn’t refresh");
  expect(screen.queryByText(/No files sent yet/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(retry).toHaveBeenCalledOnce();
});
