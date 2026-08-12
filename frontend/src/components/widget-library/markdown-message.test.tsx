import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MarkdownMessage } from "@/components/widget-library/markdown-message";

describe("MarkdownMessage", () => {
  it("renders GFM tables and narrative structure", () => {
    const { container } = render(<MarkdownMessage>{"**Summary**\n\n| Category | Spend |\n|---|---:|\n| Food | ₹500 |"}</MarkdownMessage>);
    expect(screen.getByText("Summary").tagName).toBe("STRONG");
    expect(container.querySelector("table")).not.toBeNull();
    expect(screen.getByText("Food")).toBeInTheDocument();
  });

  it("does not render raw HTML or unsafe links", () => {
    const { container } = render(<MarkdownMessage>{"<script>alert('x')</script>\n\n[unsafe](javascript:alert(1))"}</MarkdownMessage>);
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText("unsafe").closest("a")).toBeNull();
  });
});
