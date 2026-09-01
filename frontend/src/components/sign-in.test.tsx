import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CodeExchange } from "@/components/sign-in";

describe("CodeExchange phone field", () => {
  it("keeps +91 fixed and submits exactly ten national-number digits", async () => {
    const onStart = vi.fn().mockResolvedValue({
      challengeId: "challenge",
      channel: "phone",
      destinationMasked: "+91•••••210",
      expiresInSeconds: 300,
      resendAfterSeconds: 45,
      debugCode: null,
    });
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });

    render(<QueryClientProvider client={queryClient}>
      <CodeExchange channel="phone" onStart={onStart} onVerify={vi.fn()} submitLabel="Sign in" />
    </QueryClientProvider>);

    const input = screen.getByRole("textbox", { name: "10-digit mobile number with country code +91" });
    const send = screen.getByRole("button", { name: "Send code" });
    expect(screen.getByText("+91")).toBeVisible();

    fireEvent.change(input, { target: { value: "0" } });
    expect(input).toHaveValue("");

    fireEvent.change(input, { target: { value: "987654321" } });
    expect(send).toBeDisabled();

    // A pasted domestic trunk prefix is removed because +91 is already fixed.
    fireEvent.change(input, { target: { value: "09876543210" } });
    expect(input).toHaveValue("9876543210");

    // Pasting the complete international number must not duplicate +91.
    fireEvent.change(input, { target: { value: "+91 98765 43210" } });
    expect(input).toHaveValue("9876543210");
    expect(send).toBeEnabled();
    fireEvent.click(send);

    await waitFor(() => expect(onStart).toHaveBeenCalledWith("+919876543210"));
  });
});

describe.each([
  { channel: "phone" as const, identifier: "9876543210", inputName: /^10-digit mobile number with country code \+91/ },
  { channel: "email" as const, identifier: "person@example.com", inputName: /^Email address/ },
])("CodeExchange $channel OTP", ({ channel, identifier, inputName }) => {
  it("automatically verifies on the sixth digit and shows the pending transition", async () => {
    let finishVerification!: () => void;
    const onVerify = vi.fn().mockImplementation(() => new Promise<void>((resolve) => { finishVerification = resolve; }));
    const onStart = vi.fn().mockResolvedValue({
      challengeId: `${channel}-challenge`,
      channel,
      destinationMasked: channel === "phone" ? "+91•••••210" : "p••••n@example.com",
      expiresInSeconds: 300,
      resendAfterSeconds: 45,
      debugCode: null,
    });
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });

    render(<QueryClientProvider client={queryClient}>
      <CodeExchange channel={channel} onStart={onStart} onVerify={onVerify} submitLabel="Sign in" />
    </QueryClientProvider>);

    fireEvent.change(screen.getByRole("textbox", { name: inputName }), { target: { value: identifier } });
    fireEvent.click(screen.getByRole("button", { name: "Send code" }));

    const codeInput = await screen.findByRole("textbox", { name: /^Six-digit code/ });
    fireEvent.change(codeInput, { target: { value: "12345" } });
    expect(onVerify).not.toHaveBeenCalled();

    fireEvent.change(codeInput, { target: { value: "123456" } });

    await waitFor(() => expect(onVerify).toHaveBeenCalledWith(`${channel}-challenge`, "123456"));
    expect(onVerify).toHaveBeenCalledTimes(1);
    expect(codeInput).toBeDisabled();
    expect(screen.getByText("Verifying your code…")).toBeVisible();
    expect(screen.getByRole("button", { name: "Checking…" })).toBeDisabled();

    await act(async () => { finishVerification(); });
  });
});
