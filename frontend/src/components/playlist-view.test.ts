import { afterEach, describe, expect, it, vi } from "vitest";
import { type PlaylistInfo } from "../api";
import { createPlaylistView } from "./playlist-view";

const samplePlaylist: PlaylistInfo = {
  playlist_id: "PLtest",
  title: "Test Playlist",
  uploader: "Tester",
  video_count: 1,
  entries: [
    {
      video_id: "abc123",
      title: "Entry One",
      duration: 60,
      thumbnail: "https://example.com/t.jpg",
    },
  ],
};

describe("createPlaylistView download button", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("opens download URL in a new tab with noopener and noreferrer", () => {
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    const view = createPlaylistView(samplePlaylist, () => ({
      fmt: "mp4",
      quality: "best",
    }));

    const button = view.querySelector<HTMLButtonElement>("button");
    expect(button).not.toBeNull();
    button!.click();

    expect(openSpy).toHaveBeenCalledTimes(1);
    const [, target, features] = openSpy.mock.calls[0];
    expect(target).toBe("_blank");
    expect(features).toBe("noopener,noreferrer");
  });
});

describe("createPlaylistView escaping", () => {
  const hostile = 'Clip " onerror="alert(1)';

  it("keeps a quote in an entry title out of the surrounding attributes", () => {
    const view = createPlaylistView(
      {
        ...samplePlaylist,
        entries: [{ ...samplePlaylist.entries[0], title: hostile }],
      },
      () => ({ fmt: "mp4", quality: "best" }),
    );

    const entryTitle = view.querySelector<HTMLParagraphElement>("li p")!;
    expect(entryTitle.getAttribute("onerror")).toBeNull();
    expect(entryTitle.title).toBe(hostile);
    expect(entryTitle.textContent).toBe(hostile);
  });

  it("keeps a quote in the playlist title out of the markup", () => {
    const view = createPlaylistView(
      { ...samplePlaylist, title: hostile },
      () => ({ fmt: "mp4", quality: "best" }),
    );

    const heading = view.querySelector("h2")!;
    expect(heading.getAttribute("onerror")).toBeNull();
    expect(heading.textContent).toBe(hostile);
  });
});
