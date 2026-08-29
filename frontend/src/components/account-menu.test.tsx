import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AccountMenu } from "@/components/account-menu";
import { appPaths } from "@/routing/paths";

const USER = {
  id: "5f1f0f6f-2b3a-4a4a-9d1a-3f8f9a0b1c2d",
  name: "Hari Shekhar",
  currency: "INR",
  timezone: "Asia/Kolkata",
};

describe("AccountMenu", () => {
  it("opens from the user row and groups quick page and account links", () => {
    const onNavigate = vi.fn();
    render(<AccountMenu user={USER} signingOut={false} onNavigate={onNavigate} onSignOut={vi.fn()} />);

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Hari Shekhar account menu" }));

    const menu = screen.getByRole("menu", { name: "Hari Shekhar account menu" });
    expect(menu).toHaveTextContent("INR · Asia/Kolkata");
    expect(within(menu).getByText("Quick access")).toBeInTheDocument();
    expect(within(menu).getByText("Your account")).toBeInTheDocument();
    expect(within(menu).getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "Overview",
      "Transactions",
      "Categories",
      "Profile & sign-in",
      "Agent settings",
      "Appearance & app",
      "Sign out",
    ]);

    fireEvent.click(within(menu).getByRole("menuitem", { name: "Profile & sign-in" }));
    expect(onNavigate).toHaveBeenCalledWith(appPaths.settings);
  });

  it("keeps sign out distinct from navigation", () => {
    const onNavigate = vi.fn();
    const onSignOut = vi.fn();
    render(<AccountMenu user={USER} signingOut={false} onNavigate={onNavigate} onSignOut={onSignOut} />);

    fireEvent.click(screen.getByRole("button", { name: "Hari Shekhar account menu" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));

    expect(onSignOut).toHaveBeenCalledOnce();
    expect(onNavigate).not.toHaveBeenCalled();
  });
});
