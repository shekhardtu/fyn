import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TransactionIdentifier } from "@/components/transaction-identifier";

describe("transaction identifier", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows one stable compact reference and the current version", () => {
    const transactionId = "16a3ff79-4035-427b-a538-6ce4bf2b608b";
    render(<TransactionIdentifier transactionId={transactionId} rowVersion={3} />);

    expect(screen.getByRole("button", { name: `Copy Transaction ID ${transactionId}` })).toHaveTextContent("TXN 16A3FF79…BF2B608B");
    expect(screen.getByText("· Version 3")).toBeInTheDocument();
  });

  it("copies the full canonical UUID, not its visual abbreviation", async () => {
    const transactionId = "16a3ff79-4035-427b-a538-6ce4bf2b608b";
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    render(<TransactionIdentifier transactionId={transactionId} rowVersion={2} />);

    fireEvent.click(screen.getByRole("button", { name: `Copy Transaction ID ${transactionId}` }));

    await waitFor(() => expect(screen.getByRole("button")).toHaveTextContent("Copied"));
    expect(writeText).toHaveBeenCalledWith(transactionId);
  });

  it("does not mislabel a draft or malformed ID as a persisted transaction", () => {
    const { container } = render(<TransactionIdentifier transactionId="draft-123" rowVersion={1} />);
    expect(container).toBeEmptyDOMElement();
  });
});
