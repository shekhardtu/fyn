import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CategoryManager, type CategoryUsage } from "@/components/category-manager";
import type { CategoryDirectoryOut } from "@/lib/protocol";

const { toastMock } = vi.hoisted(() => ({ toastMock: { add: vi.fn(), close: vi.fn() } }));

vi.mock("@/components/ui/toast", () => ({ toast: toastMock, UNDO_WINDOW_MS: 7000 }));

type UndoSlip = {
  title: string;
  description: string;
  actionProps: { onClick: () => void };
  onClose: () => void;
};

const categories: CategoryDirectoryOut[] = [{
  id: "42b9db9a-ff04-4ffc-b428-82bb3fb1eb80",
  slug: "custom-pets",
  label: "Pets",
  icon: "circle-ellipsis",
  editable: true,
  subcategories: [{ id: "7d9b7570-1e89-4dcb-b0ad-d9dbbd0c0432", slug: "vet", label: "Vet", editable: true }],
  hints: [],
}];

function props() {
  const usage = new Map<string, CategoryUsage>([[categories[0].id, { amountMinor: 3_000, count: 1, sharePercent: 100, subcategories: new Map() }]]);
  return {
    categories,
    usage,
    currency: "INR",
    onCreateCategory: vi.fn().mockResolvedValue(categories[0]),
    onRenameCategory: vi.fn().mockResolvedValue(undefined),
    onDeleteCategory: vi.fn().mockResolvedValue(undefined),
    onCreateSubcategory: vi.fn().mockResolvedValue(categories[0].subcategories[0]),
    onRenameSubcategory: vi.fn().mockResolvedValue(undefined),
    onDeleteSubcategory: vi.fn().mockResolvedValue(undefined),
    onCreateHint: vi.fn().mockResolvedValue({ id: "hint", merchant: "Uber", categoryId: categories[0].id, subcategoryId: null, subcategory: null }),
    onUpdateHint: vi.fn().mockResolvedValue(undefined),
    onDeleteHint: vi.fn().mockResolvedValue(undefined),
  };
}

describe("CategoryManager", () => {
  beforeEach(() => {
    toastMock.add.mockClear();
    toastMock.close.mockClear();
  });

  it("creates subcategories and transaction hints from the selected category", async () => {
    const callbacks = props();
    render(<CategoryManager {...callbacks} />);

    fireEvent.click(screen.getByRole("button", { name: "Add subcategory" }));
    const nameInput = screen.getByLabelText("subcategory name");
    expect(nameInput.className).toContain("manual-field");
    expect(nameInput.className).not.toContain("focus-visible:ring");
    fireEvent.change(nameInput, { target: { value: "Grooming" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(callbacks.onCreateSubcategory).toHaveBeenCalledWith(categories[0].id, "Grooming"));

    fireEvent.click(screen.getByRole("button", { name: "Add hint" }));
    fireEvent.change(screen.getByLabelText("Merchant hint"), { target: { value: "Heads Up For Tails" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(callbacks.onCreateHint).toHaveBeenCalledWith(categories[0].id, "Heads Up For Tails", null));
  });

  it("explains an in-use category instead of sending a delete", () => {
    const callbacks = props();
    render(<CategoryManager {...callbacks} />);
    fireEvent.click(screen.getByRole("button", { name: "Delete Pets" }));

    expect(screen.getByRole("status")).toHaveTextContent("Pets is categorizing 1 transaction");
    expect(callbacks.onDeleteCategory).not.toHaveBeenCalled();
    expect(toastMock.add).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Got it" }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("deletes an unused subcategory when its undo window closes", async () => {
    const callbacks = props();
    render(<CategoryManager {...callbacks} />);
    fireEvent.click(screen.getByRole("button", { name: "Delete Vet" }));

    // The row leaves at once; the API waits for the slip to close un-undone.
    expect(screen.queryByText("Vet")).not.toBeInTheDocument();
    expect(callbacks.onDeleteSubcategory).not.toHaveBeenCalled();
    const slip = toastMock.add.mock.calls[0][0] as UndoSlip;
    expect(slip.description).toBe("Vet");
    slip.onClose();
    // The row stays withheld until the API answers; in the app the refetch
    // then removes it for real, which a static fixture can't show.
    await waitFor(() => expect(callbacks.onDeleteSubcategory).toHaveBeenCalledWith(categories[0].id, categories[0].subcategories[0].id));
  });

  it("puts the row back on undo without calling the API", async () => {
    const callbacks = props();
    render(<CategoryManager {...callbacks} />);
    fireEvent.click(screen.getByRole("button", { name: "Delete Vet" }));

    const slip = toastMock.add.mock.calls[0][0] as UndoSlip;
    slip.actionProps.onClick();
    expect(toastMock.close).toHaveBeenCalledWith(categories[0].subcategories[0].id);
    slip.onClose();
    await waitFor(() => expect(screen.getByText("Vet")).toBeVisible());
    expect(callbacks.onDeleteSubcategory).not.toHaveBeenCalled();
  });
});
