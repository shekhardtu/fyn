import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ConversationTitle } from "@/components/conversation-title";

describe("ConversationTitle", () => {
  it("keeps the browser title in sync with the active conversation", () => {
    const appTitle = document.createElement("title");
    appTitle.textContent = "fyn AI";
    document.head.append(appTitle);

    const view = render(<ConversationTitle title="August spending review" />);
    expect(document.title).toBe("August spending review");

    view.rerender(<ConversationTitle title="Savings plan" />);
    expect(document.title).toBe("Savings plan");

    view.unmount();
    // Route metadata owns the title after navigation; unmounting this helper
    // must not race it by writing another title during cleanup.
    expect(document.title).toBe("Savings plan");
    appTitle.remove();
  });

  it("uses the conversation fallback for an empty title", () => {
    const view = render(<ConversationTitle title="  " />);
    expect(document.title).toBe("Financial check-in");
    view.unmount();
  });
});
