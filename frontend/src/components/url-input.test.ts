import { describe, expect, it } from "vitest";
import { createUrlInput } from "./url-input";

describe("createUrlInput accessibility", () => {
  it("associates the input with a label and the error region", () => {
    const view = createUrlInput(() => undefined);

    const label = view.querySelector<HTMLLabelElement>('label[for="url-input"]');
    const input = view.querySelector<HTMLInputElement>("#url-input");
    const errorEl = view.querySelector<HTMLElement>("#url-error");

    expect(label).not.toBeNull();
    expect(input).not.toBeNull();
    expect(input!.getAttribute("aria-describedby")).toBe("url-error");
    expect(errorEl).not.toBeNull();
    expect(errorEl!.getAttribute("role")).toBe("status");
    expect(errorEl!.getAttribute("aria-live")).toBe("polite");
  });

  it("toggles aria-invalid as the value validity changes", () => {
    const view = createUrlInput(() => undefined);
    const input = view.querySelector<HTMLInputElement>("#url-input")!;

    expect(input.getAttribute("aria-invalid")).toBe("false");

    input.value = "not a url";
    input.dispatchEvent(new Event("input"));
    expect(input.getAttribute("aria-invalid")).toBe("true");

    input.value = "https://www.youtube.com/watch?v=abc";
    input.dispatchEvent(new Event("input"));
    expect(input.getAttribute("aria-invalid")).toBe("false");
  });

  it("accepts an uppercase host", () => {
    const view = createUrlInput(() => undefined);
    const input = view.querySelector<HTMLInputElement>("#url-input")!;

    input.value = "https://WWW.YouTube.com/watch?v=abc";
    input.dispatchEvent(new Event("input"));

    expect(input.getAttribute("aria-invalid")).toBe("false");
  });
});
