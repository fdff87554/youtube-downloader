import { describe, expect, it } from "vitest";
import { createFormatPicker } from "./format-picker";

describe("createFormatPicker accessibility", () => {
  it("connects each select to its visible label via for/id", () => {
    const view = createFormatPicker(() => undefined);

    const formatLabel = view.querySelector<HTMLLabelElement>(
      'label[for="format-select"]',
    );
    const formatSelect = view.querySelector<HTMLSelectElement>("#format-select");
    const qualityLabel = view.querySelector<HTMLLabelElement>(
      'label[for="quality-select"]',
    );
    const qualitySelect = view.querySelector<HTMLSelectElement>("#quality-select");

    expect(formatLabel).not.toBeNull();
    expect(formatSelect).not.toBeNull();
    expect(qualityLabel).not.toBeNull();
    expect(qualitySelect).not.toBeNull();
  });
});

describe("createFormatPicker quality options", () => {
  it("labels best as the most compatible choice, not the highest resolution", () => {
    // The backend prefers H.264 for compatibility, so best usually tops
    // out at 1080p even when a 4K AV1 rendition exists.
    const view = createFormatPicker(() => undefined);

    const best = view.querySelector<HTMLOptionElement>(
      '#quality-select option[value="best"]',
    );

    expect(best?.textContent).toBe("Best (most compatible)");
  });
});
