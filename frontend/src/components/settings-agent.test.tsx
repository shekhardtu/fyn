import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AnswerStyleChoices, AnswerValidationChoices } from "@/components/settings-agent";

describe("agent answer settings", () => {
  it("shows the saved mode and sends only a changed choice", () => {
    const onChange = vi.fn();
    render(<AnswerValidationChoices value="full" onChange={onChange} />);

    expect(screen.getByRole("radio", { name: /Full/ })).toHaveAttribute("aria-checked", "true");
    fireEvent.click(screen.getByRole("radio", { name: /Full/ }));
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("radio", { name: /Evidence only/ }));
    expect(onChange).toHaveBeenCalledWith("evidence_only");
  });

  it("offers explained and concise answer styles", () => {
    const onChange = vi.fn();
    render(<AnswerStyleChoices value="explained" onChange={onChange} />);

    expect(screen.getByRole("radio", { name: /Explained/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText(/Even a simple lookup gets one or two useful sentences/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: /Concise/ }));
    expect(onChange).toHaveBeenCalledWith("concise");
  });
});
