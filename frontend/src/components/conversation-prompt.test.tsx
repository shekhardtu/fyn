import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode, useCallback, useState } from "react";
import { createMemoryRouter } from "react-router";
import { RouterProvider } from "react-router/dom";
import { describe, expect, it, vi } from "vitest";
import { useConversationPrompt } from "@/components/conversation-prompt";
import { appPaths } from "@/routing/paths";

function renderPrompt(entry: string, { ready = true, draft = "" } = {}) {
  const onSend = vi.fn();
  function Composer() {
    const [loaded, setLoaded] = useState(ready);
    const [input, setInput] = useState(draft);
    useConversationPrompt(loaded, useCallback((prompt: string) => {
      setInput((current) => current ? `${current}\n\n${prompt}` : prompt);
    }, []));
    return <>
      <textarea aria-label="Message" value={input} onChange={(event) => setInput(event.target.value)} />
      <button onClick={() => onSend(input)}>Send</button>
      <button onClick={() => setLoaded(true)}>Finish loading</button>
    </>;
  }
  const router = createMemoryRouter([{ path: "/c/:id", element: <Composer /> }], { initialEntries: [entry] });
  render(<StrictMode><RouterProvider router={router} /></StrictMode>);
  return { router, onSend };
}

describe("conversation prompt links", () => {
  it("waits for the thread, prefills once, and leaves sending to the user", async () => {
    const prompt = "Set a ₹20,000 budget for Food & travel?";
    const { router, onSend } = renderPrompt(`${appPaths.conversation("thread / 2", prompt)}&keep=1`, { ready: false });
    expect(screen.getByRole("textbox")).toHaveValue("");
    expect(new URLSearchParams(router.state.location.search).get("prompt")).toBe(prompt);

    fireEvent.click(screen.getByRole("button", { name: "Finish loading" }));
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue(prompt));
    expect(router.state.location.search).toBe("?keep=1");
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Set my budget to ₹15,000 instead." } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onSend).toHaveBeenCalledWith("Set my budget to ₹15,000 instead.");
  });

  it("accepts another link to the same mounted thread and preserves an existing draft", async () => {
    const { router } = renderPrompt(appPaths.conversation("one", "Help me add an account."), { draft: "My draft" });
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("My draft\n\nHelp me add an account."));

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "A new draft" } });
    await act(() => router.navigate(appPaths.conversation("one", "Help me create a savings goal.")));
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("A new draft\n\nHelp me create a savings goal."));
    expect(router.state.location.search).toBe("");
  });

  it("leaves ordinary conversation links and blank suggestions alone", async () => {
    expect(appPaths.conversation("one")).toBe("/c/one");
    expect(appPaths.conversation("one", "  ")).toBe("/c/one");
    const { router, onSend } = renderPrompt("/c/one?prompt=%20%20", { draft: "Keep this" });
    await waitFor(() => expect(router.state.location.search).toBe(""));
    expect(screen.getByRole("textbox")).toHaveValue("Keep this");
    expect(onSend).not.toHaveBeenCalled();
  });
});
