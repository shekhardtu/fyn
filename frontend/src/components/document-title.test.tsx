import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DocumentTitle } from "@/components/document-title";

describe("DocumentTitle", () => {
  it("keeps the browser title in sync with the active conversation", () => {
    const appTitle = document.createElement("title");
    appTitle.textContent = "fyn AI";
    document.head.append(appTitle);

    const view = render(<DocumentTitle title="August spending review" />);
    expect(document.title).toBe("August spending review");

    view.rerender(<DocumentTitle title="Savings plan" />);
    expect(document.title).toBe("Savings plan");

    view.unmount();
    // The next route owns the next title; cleanup must not race it by writing.
    expect(document.title).toBe("Savings plan");
    appTitle.remove();
  });

  it("uses the conversation fallback for an empty title", () => {
    const view = render(<DocumentTitle title="  " fallback="Financial check-in" />);
    expect(document.title).toBe("Financial check-in");
    view.unmount();
  });
});
