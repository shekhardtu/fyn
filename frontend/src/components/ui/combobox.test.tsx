import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Combobox } from "@/components/ui/combobox";

const options = [
  { value: "streaming", label: "Streaming" },
  { value: "movies", label: "Movies" },
  { value: "music", label: "Music" },
  { value: "games", label: "Games" },
  { value: "events", label: "Events" },
  { value: "hobbies", label: "Hobbies" },
];

describe("Combobox", () => {
  it("opens on the trigger, filters by the search field, and reports the pick", () => {
    const onValueChange = vi.fn();
    render(<Combobox aria-label="Subcategory" value="streaming" onValueChange={onValueChange} options={options} />);

    const trigger = screen.getByRole("combobox", { name: "Subcategory" });
    expect(trigger).toHaveTextContent("Streaming");
    fireEvent.click(trigger);
    expect(screen.getAllByRole("option")).toHaveLength(options.length);

    fireEvent.change(screen.getByPlaceholderText("Search…"), { target: { value: "mo" } });
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual(["Movies"]);

    fireEvent.click(screen.getByRole("option", { name: "Movies" }));
    expect(onValueChange).toHaveBeenCalledWith("movies");
  });

  it("says when nothing matches, and never offers Add without an onCreate", () => {
    render(<Combobox aria-label="Subcategory" value="" onValueChange={() => undefined} options={options} />);
    fireEvent.click(screen.getByRole("combobox", { name: "Subcategory" }));
    fireEvent.change(screen.getByPlaceholderText("Search…"), { target: { value: "zzz" } });
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByText("No matches.")).toBeInTheDocument();
  });

  it("offers an Add row for a new name but not for an existing one", () => {
    const onCreate = vi.fn();
    render(<Combobox aria-label="Subcategory" value="" onValueChange={() => undefined} options={options} onCreate={onCreate} />);
    fireEvent.click(screen.getByRole("combobox", { name: "Subcategory" }));

    // An exact match, whatever the case, is selected — not recreated.
    fireEvent.change(screen.getByPlaceholderText("Search…"), { target: { value: "STREAMING" } });
    expect(screen.queryByRole("option", { name: /^Add/ })).toBeNull();

    fireEvent.change(screen.getByPlaceholderText("Search…"), { target: { value: "Anime" } });
    fireEvent.click(screen.getByRole("option", { name: /Add “Anime”/ }));
    expect(onCreate).toHaveBeenCalledWith("Anime");
  });

  it("hides the search field for short lists that cannot grow", () => {
    render(<Combobox aria-label="Nature" value="" onValueChange={() => undefined} options={options.slice(0, 3)} />);
    fireEvent.click(screen.getByRole("combobox", { name: "Nature" }));
    expect(screen.queryByPlaceholderText("Search…")).toBeNull();
    expect(screen.getAllByRole("option")).toHaveLength(3);
  });
});
