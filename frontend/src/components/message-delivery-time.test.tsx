import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MessageDeliveryTime } from "@/components/message-delivery-time";

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
});
