import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
