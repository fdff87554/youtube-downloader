import { describe, expect, it } from "vitest";
import type { VideoInfo } from "../api";
import { createVideoInfo } from "./video-info";

// Real YouTube titles contain plain double quotes (for example
// `Interpol - "Say Hello to the Angels" (Official Audio)`), and an
// attacker controls the title of a video they ask a victim to paste in.
const HOSTILE_TITLE = 'Song " onerror="alert(1)" data-x="';

function videoInfo(overrides: Partial<VideoInfo> = {}): VideoInfo {
  return {
    video_id: "abc123",
    title: "A Title",
    thumbnail: "https://i.ytimg.com/vi/abc123/hqdefault.jpg",
    duration: 95,
    uploader: "A Channel",
    formats: [],
    ...overrides,
  };
}

describe("createVideoInfo", () => {
  it("renders title, uploader and duration", () => {
    const el = createVideoInfo(videoInfo());

    expect(el.querySelector("h2")?.textContent).toBe("A Title");
    expect(el.textContent).toContain("A Channel");
    expect(el.textContent).toContain("1:35");
  });

  it("keeps a quote in the title out of the surrounding attributes", () => {
    const el = createVideoInfo(videoInfo({ title: HOSTILE_TITLE }));

    const img = el.querySelector("img")!;
    const heading = el.querySelector("h2")!;
    expect(img.getAttribute("onerror")).toBeNull();
    expect(heading.getAttribute("onerror")).toBeNull();
    expect(img.getAttribute("data-x")).toBeNull();
    // The title survives intact as data rather than being neutered.
    expect(img.alt).toBe(HOSTILE_TITLE);
    expect(heading.title).toBe(HOSTILE_TITLE);
    expect(heading.textContent).toBe(HOSTILE_TITLE);
  });

  it("keeps a quote in the uploader name out of the markup", () => {
    const el = createVideoInfo(
      videoInfo({ uploader: '" onmouseover="alert(1)' }),
    );

    expect(el.querySelector("p")?.getAttribute("onmouseover")).toBeNull();
    expect(el.textContent).toContain('" onmouseover="alert(1)');
  });
});
