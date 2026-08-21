import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MarkdownMessage } from "@/components/widget-library/markdown-message";
import { setTablesWide } from "@/lib/wide-tables";

/* Node 24 injects its own method-less `localStorage` global that shadows
 * jsdom's, so persistence is exercised against a real in-memory Storage. */
const backing = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (key: string) => backing.get(key) ?? null,
  setItem: (key: string, value: string) => void backing.set(key, value),
  removeItem: (key: string) => void backing.delete(key),
  clear: () => backing.clear(),
});

afterEach(() => {
  // The width preference is one shared store for the whole app, so a test that
  // turns it on hands it to the next one unless it puts it back.
  act(() => setTablesWide(false));
  localStorage.clear();
});

/** Markdown for a table with `rows` body rows, one label column and one of
 *  figures, with nothing said about alignment. */
function ledger(rows: number) {
  const body = Array.from({ length: rows }, (_, index) => `| Row ${index + 1} | ₹${(index + 1) * 100} |`).join("\n");
  return `| Category | Spend |\n|---|---|\n${body}`;
}

function cellsOf(text: string) {
  return screen.getByRole("cell", { name: text });
}

describe("the table in an answer", () => {
  it("lines a column of figures up on the right, and words on the left", () => {
    render(<MarkdownMessage>{ledger(2)}</MarkdownMessage>);

    expect(cellsOf("Row 1")).toHaveAttribute("data-align", "start");
    expect(cellsOf("₹100")).toHaveAttribute("data-align", "end");
    // Tabular digits and no wrapping are the point of naming the column at all.
    expect(cellsOf("₹100")).toHaveAttribute("data-figure");
    expect(cellsOf("Row 1")).not.toHaveAttribute("data-figure");
    // The header follows its column, or the two do not read as one thing.
    expect(screen.getByRole("columnheader", { name: "Spend" })).toHaveAttribute("data-align", "end");
  });

  it("keeps the alignment the Markdown asked for over the one it would infer", () => {
    render(<MarkdownMessage>{"| Left | Middle |\n|---:|:---:|\n| 10 | words |"}</MarkdownMessage>);

    // Figures that were told to sit left stay left; words told to centre do.
    expect(cellsOf("10")).toHaveAttribute("data-align", "end");
    expect(cellsOf("words")).toHaveAttribute("data-align", "center");
  });

  it("does not read a date or a word as a figure", () => {
    render(<MarkdownMessage>{"| When | What |\n|---|---|\n| 2026-08-19 | Rent |\n| 2026-08-20 | Fuel |"}</MarkdownMessage>);

    expect(cellsOf("2026-08-19")).toHaveAttribute("data-align", "start");
    expect(cellsOf("2026-08-19")).not.toHaveAttribute("data-figure");
  });

  it("shows a long table folded, says how much it is holding back, and opens on one press", () => {
    render(<MarkdownMessage>{ledger(214)}</MarkdownMessage>);

    expect(screen.getAllByRole("row")).toHaveLength(51); // 50 rows and the header
    expect(screen.getByText("Showing 50 of 214 rows")).toBeInTheDocument();
    expect(screen.queryByRole("cell", { name: "Row 51" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Show all 214/ }));

    expect(screen.getAllByRole("row")).toHaveLength(215);
    expect(screen.getByRole("cell", { name: "Row 214" })).toBeInTheDocument();
    expect(screen.getByText("All 214 rows")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Show first 50/ }));

    expect(screen.getAllByRole("row")).toHaveLength(51);
  });

  it("leaves a table that fits whole, with no fold to press", () => {
    render(<MarkdownMessage>{ledger(50)}</MarkdownMessage>);

    expect(screen.getAllByRole("row")).toHaveLength(51);
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
    expect(screen.getByText("All 50 rows")).toBeInTheDocument();
  });

  it("brings no chrome at all to a table short enough to read whole", () => {
    render(<MarkdownMessage>{ledger(4)}</MarkdownMessage>);

    expect(screen.queryByRole("textbox", { name: "Filter rows" })).toBeNull();
    expect(screen.queryByText(/rows$/)).toBeNull();
  });

  it("gives the scrollable region a name a reader can act on, and the keyboard a way in", () => {
    render(<MarkdownMessage>{ledger(2)}</MarkdownMessage>);

    const region = screen.getByRole("region", { name: "Table: Category, Spend" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(within(region).getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Category" })).toHaveAttribute("scope", "col");
  });
});

describe("how wide tables are drawn", () => {
  it("widens every table in the thread from any one of their controls, and remembers it", () => {
    render(<><MarkdownMessage>{ledger(2)}</MarkdownMessage><MarkdownMessage>{ledger(3)}</MarkdownMessage></>);
    const frames = document.querySelectorAll(".ledger-table");
    expect(frames).toHaveLength(2);
    expect([...frames].every((frame) => frame.getAttribute("data-wide") === null)).toBe(true);

    fireEvent.click(screen.getAllByRole("button", { name: /Widen every table/ })[0]);

    expect([...document.querySelectorAll(".ledger-table")].every((frame) => frame.getAttribute("data-wide") === "true")).toBe(true);
    expect(localStorage.getItem("fyn.tables-wide")).toBe("1");
    // Both controls now offer the way back, not just the one that was pressed.
    expect(screen.getAllByRole("button", { name: /Narrow every table/ })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: /Narrow every table/ })[0]).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getAllByRole("button", { name: /Narrow every table/ })[1]);

    expect([...document.querySelectorAll(".ledger-table")].every((frame) => frame.getAttribute("data-wide") === null)).toBe(true);
    expect(localStorage.getItem("fyn.tables-wide")).toBeNull();
  });

  it("comes back wide after a reload", async () => {
    act(() => setTablesWide(true));
    vi.resetModules();

    const reloaded = await import("@/lib/wide-tables");

    expect(reloaded.tablesWide()).toBe(true);
  });
});

/** The first column of every body row on screen, in the order they are drawn. */
function labels() {
  return screen.getAllByRole("row").slice(1).map((row) => within(row).getAllByRole("cell")[0]?.textContent ?? "");
}

describe("reading a table another way round", () => {
  const spend = "| Category | Spend |\n|---|---|\n| Fuel | ₹300 |\n| Rent | ₹1,200 |\n| Food | ₹50 |\n| Total | ₹1,550 |";

  it("sorts a column of figures largest first, then smallest, then back to the answer's own order", () => {
    render(<MarkdownMessage>{spend}</MarkdownMessage>);
    expect(labels()).toEqual(["Fuel", "Rent", "Food", "Total"]);

    const spendColumn = screen.getByRole("button", { name: "Spend" });
    fireEvent.click(spendColumn);
    expect(labels()).toEqual(["Rent", "Fuel", "Food", "Total"]);
    expect(screen.getByRole("columnheader", { name: /Spend/ })).toHaveAttribute("aria-sort", "descending");

    fireEvent.click(spendColumn);
    expect(labels()).toEqual(["Food", "Fuel", "Rent", "Total"]);
    expect(screen.getByRole("columnheader", { name: /Spend/ })).toHaveAttribute("aria-sort", "ascending");

    fireEvent.click(spendColumn);
    expect(labels()).toEqual(["Fuel", "Rent", "Food", "Total"]);
    expect(screen.getByRole("columnheader", { name: /Spend/ })).toHaveAttribute("aria-sort", "none");
  });

  it("sorts words A to Z and keeps the total at the foot either way", () => {
    render(<MarkdownMessage>{spend}</MarkdownMessage>);

    fireEvent.click(screen.getByRole("button", { name: "Category" }));
    expect(labels()).toEqual(["Food", "Fuel", "Rent", "Total"]);

    fireEvent.click(screen.getByRole("button", { name: "Category" }));
    expect(labels()).toEqual(["Rent", "Fuel", "Food", "Total"]);
  });

  it("filters to the rows that match, says how many, and offers the way back", () => {
    render(<MarkdownMessage>{ledger(60)}</MarkdownMessage>);

    fireEvent.change(screen.getByRole("textbox", { name: "Filter rows" }), { target: { value: "Row 7" } });

    expect(labels()).toEqual(["Row 7"]);
    expect(screen.getByText("1 of 60 rows match “Row 7”")).toBeInTheDocument();

    fireEvent.change(screen.getByRole("textbox", { name: "Filter rows" }), { target: { value: "nothing here" } });

    // The header stands over no rows and the footer says why, once.
    expect(labels()).toEqual([]);
    expect(screen.getByText("No rows match “nothing here”")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Clear the filter" }));

    // Back to all sixty, which the fold trims to the first fifty again.
    expect(labels()).toHaveLength(50);
    expect(screen.getByText("Showing 50 of 60 rows")).toBeInTheDocument();
  });

  it("copies the table back as the Markdown it arrived as", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    // jsdom ships no clipboard at all, so one is defined rather than stubbed —
    // replacing the whole navigator would take the storage stub with it.
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(<MarkdownMessage>{spend}</MarkdownMessage>);

    fireEvent.click(screen.getByRole("button", { name: "Copy as Markdown" }));

    // Right-aligned in the copy because that is how the column is drawn here.
    expect(writeText).toHaveBeenCalledWith([
      "| Category | Spend |",
      "| --- | ---: |",
      "| Fuel | ₹300 |",
      "| Rent | ₹1,200 |",
      "| Food | ₹50 |",
      "| Total | ₹1,550 |",
    ].join("\n"));
    expect(await screen.findByRole("button", { name: "Copied 4 rows" })).toBeInTheDocument();
  });
});

describe("the total row", () => {
  it("is the last row, labelled, and figures the rest of the way — not any row that says total", () => {
    // A real merchant. Sorting it to the foot of every table and exempting it
    // from the fold would be a bug nobody would think to look for.
    render(<MarkdownMessage>{"| Merchant | Amount | Status |\n|---|---|---|\n| Total Wine & More | ₹4,300 | Confirmed |\n| Swiggy | ₹800 | Pending |"}</MarkdownMessage>);

    expect(labels()).toEqual(["Total Wine & More", "Swiggy"]);
    fireEvent.click(screen.getByRole("button", { name: "Amount" }));
    // Sorted by amount, largest first — the merchant sorts like any other row.
    expect(labels()).toEqual(["Total Wine & More", "Swiggy"]);
    fireEvent.click(screen.getByRole("button", { name: "Amount" }));
    expect(labels()).toEqual(["Swiggy", "Total Wine & More"]);
  });

  it("survives the fold, however long the table is", () => {
    const rows = Array.from({ length: 80 }, (_, index) => `| Row ${index + 1} | ${index + 1} |`).join("\n");
    render(<MarkdownMessage>{`| Category | Spend |\n|---|---|\n${rows}\n| Total | 3,240 |`}</MarkdownMessage>);

    expect(screen.getByRole("cell", { name: "Total" })).toBeInTheDocument();
    expect(screen.queryByRole("cell", { name: "Row 51" })).toBeNull();
    expect(labels()).toHaveLength(51);
  });
});

describe("the measure", () => {
  function measures() {
    return [...document.querySelectorAll(".ledger-measure")].map((mark) => (mark as HTMLElement).style.getPropertyValue("--measure"));
  }

  it("scales each figure against the largest real row, and leaves the total out of it", () => {
    render(<MarkdownMessage>{"| Category | Spend |\n|---|---|\n| A | 100 |\n| B | 50 |\n| C | 25 |\n| D | 0 |\n| E | 25 |\n| Total | 200 |"}</MarkdownMessage>);

    expect(measures()).toEqual(["1", "0.5", "0.25", "0", "0.25"]);
  });

  it("never draws against a column of years, where every rule would be full", () => {
    render(<MarkdownMessage>{"| Year | Spend |\n|---|---|\n| 2022 | 100 |\n| 2023 | 50 |\n| 2024 | 25 |\n| 2025 | 40 |\n| 2026 | 60 |"}</MarkdownMessage>);

    // One rule per row, from the spend column alone.
    expect(measures()).toEqual(["1", "0.5", "0.25", "0.4", "0.6"]);
  });

  it("stays away from a table too short to compare", () => {
    render(<MarkdownMessage>{ledger(4)}</MarkdownMessage>);

    expect(measures()).toEqual([]);
  });

  it("stays away from a column that goes negative, where a length cannot say so", () => {
    render(<MarkdownMessage>{"| Category | Change |\n|---|---|\n| A | 10 |\n| B | -20 |\n| C | 30 |\n| D | 5 |\n| E | 15 |"}</MarkdownMessage>);

    expect(measures()).toEqual([]);
  });
});

describe("a table the transcript has unmounted and mounted again", () => {
  const long = ledger(80);

  it("comes back sorted, filtered and unfolded the way it was left", () => {
    const { unmount } = render(<MarkdownMessage id="msg-1">{long}</MarkdownMessage>);
    fireEvent.click(screen.getByRole("button", { name: /Show all/ }));
    fireEvent.click(screen.getByRole("button", { name: "Category" }));
    expect(labels()).toHaveLength(80);
    expect(labels()[0]).toBe("Row 1");

    // What the virtualiser does to a turn eight rows out of the window.
    unmount();
    render(<MarkdownMessage id="msg-1">{long}</MarkdownMessage>);

    expect(labels()).toHaveLength(80);
    expect(screen.getByRole("columnheader", { name: /Category/ })).toHaveAttribute("aria-sort", "ascending");
  });

  it("keeps one message's table apart from another's", () => {
    render(<><MarkdownMessage id="msg-2">{long}</MarkdownMessage><MarkdownMessage id="msg-3">{long}</MarkdownMessage></>);

    fireEvent.click(screen.getAllByRole("button", { name: /Show all/ })[0]);

    const [first, second] = [...document.querySelectorAll(".ledger-table")].map((frame) => frame.querySelectorAll("tbody tr").length);
    expect([first, second]).toEqual([80, 50]);
  });
});
