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

  it("labels fixed qualities as ceilings, since H.264 may stop lower", () => {
    // A video with 1080p AV1 but H.264 only up to 720p downloads at
    // 720p for "1080", so the label must not promise 1080p.
    const view = createFormatPicker(() => undefined);

    const labels = ["1080", "720", "480"].map(
      (value) =>
        view.querySelector<HTMLOptionElement>(
          `#quality-select option[value="${value}"]`,
        )?.textContent,
    );

    expect(labels).toEqual(["Up to 1080p", "Up to 720p", "Up to 480p"]);
  });
});
