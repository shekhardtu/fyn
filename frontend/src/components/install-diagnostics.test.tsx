import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { InstallDiagnostics } from "@/components/install-diagnostics";

function setBuildMeta(content: string) {
  const meta = document.createElement("meta");
  meta.name = "application-version";
  meta.content = content;
  document.head.append(meta);
}

afterEach(() => {
  document.head.querySelectorAll('meta[name="application-version"]').forEach((node) => node.remove());
});

describe("InstallDiagnostics", () => {
  it("reports the build the page was served with", () => {
    setBuildMeta("0.1.0+27f177a07e1a");
    render(<InstallDiagnostics />);
    expect(screen.getByText("0.1.0+27f177a07e1a")).toBeInTheDocument();
  });

  it("says so rather than showing a placeholder no build ever replaced", () => {
    setBuildMeta("__FYN_BUILD_VERSION__");
    render(<InstallDiagnostics />);
    expect(screen.getByText("unknown")).toBeInTheDocument();
  });

  it("keeps the measurements and the keyboard test behind a press", () => {
    setBuildMeta("0.1.0+27f177a07e1a");
    render(<InstallDiagnostics />);
    expect(screen.queryByRole("textbox", { name: "Keyboard test field" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show screen measurements" }));

    expect(screen.getByRole("textbox", { name: "Keyboard test field" })).toBeInTheDocument();
    expect(screen.getByText("visual offsetTop")).toBeInTheDocument();
    expect(screen.getByText("published offset")).toBeInTheDocument();
  });
});
