import { beforeEach, describe, expect, it } from "vitest";
import { takeSharedText } from "@/lib/share-target";

function navigateTo(url: string) {
  window.history.replaceState(null, "", url);
}

beforeEach(() => navigateTo("/"));

describe("takeSharedText", () => {
  it("is empty on an ordinary navigation, and leaves the URL alone", () => {
    navigateTo("/?type=expense");
    expect(takeSharedText()).toBe("");
    expect(window.location.search).toBe("?type=expense");
  });

  it("joins the parts a share sheet sends", () => {
    navigateTo("/?title=Receipt&text=Paid%20%E2%82%B9450%20at%20Blue%20Tokai");
    expect(takeSharedText()).toBe("Receipt Paid ₹450 at Blue Tokai");
  });

  it("does not repeat a link sent as both text and url", () => {
    navigateTo("/?text=https%3A%2F%2Fexample.com%2Fr%2F1&url=https%3A%2F%2Fexample.com%2Fr%2F1");
    expect(takeSharedText()).toBe("https://example.com/r/1");
  });

  it("consumes the parameters so a refresh does not re-seed the composer", () => {
    navigateTo("/?text=Lunch&type=expense");
    expect(takeSharedText()).toBe("Lunch");
    expect(window.location.search).toBe("?type=expense");
    expect(takeSharedText()).toBe("");
  });

  it("caps what one share can push into the composer", () => {
    navigateTo(`/?text=${"x".repeat(5_000)}`);
    expect(takeSharedText()).toHaveLength(2_000);
  });
});
