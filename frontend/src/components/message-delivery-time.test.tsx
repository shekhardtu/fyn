import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MessageDeliveryTime } from "@/components/message-delivery-time";
import { UserDefaultsProvider } from "@/components/user-defaults";

describe("message delivery time", () => {
  it("renders the persisted instant in the browser's local timezone", () => {
    const deliveredAt = "2026-08-16T10:51:31.799Z";
    const expected = new Date(deliveredAt).toLocaleString("en-IN", {
      dateStyle: "medium",
      timeStyle: "short",
    });

    render(<MessageDeliveryTime deliveredAt={deliveredAt} />);

    const timestamp = screen.getByLabelText(`Delivered ${expected}, local time`);
    expect(timestamp).toHaveTextContent(`Delivered ${expected}`);
    expect(timestamp).toHaveAttribute("datetime", deliveredAt);
  });

  it("does not invent a timestamp when delivery has not completed", () => {
    const { container } = render(<MessageDeliveryTime deliveredAt="" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders delivery using the timezone saved on the profile", () => {
    const deliveredAt = "2026-08-16T23:30:00.000Z";
    const expected = new Date(deliveredAt).toLocaleString("en-IN", {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: "Asia/Kolkata",
    });

    render(<UserDefaultsProvider value={{ currency: "USD", timeZone: "Asia/Kolkata" }}>
      <MessageDeliveryTime deliveredAt={deliveredAt} />
    </UserDefaultsProvider>);

    expect(screen.getByLabelText(`Delivered ${expected}, Asia/Kolkata time`)).toHaveTextContent(`Delivered ${expected}`);
  });
});
