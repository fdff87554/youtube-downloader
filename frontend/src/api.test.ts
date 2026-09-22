import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  buildDownloadUrl,
  FETCH_INFO_TIMEOUT_MS,
  fetchInfo,
  formatDuration,
  formatFileSize,
  isPlaylistInfo,
  isVideoInfo,
  type PlaylistInfo,
  type VideoInfo,
} from "./api";

const sampleVideo: VideoInfo = {
  video_id: "v1",
  title: "Test",
  thumbnail: "https://example.com/t.jpg",
  duration: 60,
  uploader: "Channel",
  formats: [],
};

const samplePlaylist: PlaylistInfo = {
  playlist_id: "p1",
  title: "List",
  uploader: "Creator",
  video_count: 0,
  entries: [],
};

describe("isVideoInfo / isPlaylistInfo", () => {
  it("identifies a video response", () => {
    expect(isVideoInfo(sampleVideo)).toBe(true);
    expect(isPlaylistInfo(sampleVideo)).toBe(false);
  });

  it("identifies a playlist response", () => {
    expect(isPlaylistInfo(samplePlaylist)).toBe(true);
    expect(isVideoInfo(samplePlaylist)).toBe(false);
  });
});

describe("formatDuration", () => {
  it("formats seconds shorter than an hour as m:ss", () => {
    expect(formatDuration(125)).toBe("2:05");
  });

  it("formats seconds longer than an hour as h:mm:ss", () => {
    expect(formatDuration(3661)).toBe("1:01:01");
  });

  it("renders zero as 0:00", () => {
    expect(formatDuration(0)).toBe("0:00");
  });
});

describe("formatFileSize", () => {
  it("returns empty string for null", () => {
    expect(formatFileSize(null)).toBe("");
  });

  it("formats megabyte-scale sizes in MB", () => {
    expect(formatFileSize(1024 * 1024 * 5)).toBe("5.0 MB");
  });

  it("formats gigabyte-scale sizes in GB", () => {
    expect(formatFileSize(1024 * 1024 * 1024 * 2.5)).toBe("2.5 GB");
  });
});

describe("buildDownloadUrl", () => {
  it("uses mp4/best by default", () => {
    const url = buildDownloadUrl("https://www.youtube.com/watch?v=x");
    expect(url).toContain("fmt=mp4");
    expect(url).toContain("quality=best");
    expect(url).not.toContain("title=");
  });

  it("includes the title query parameter when provided", () => {
    const url = buildDownloadUrl(
      "https://www.youtube.com/watch?v=x",
      "mp3",
      "best",
      "My Video",
    );
    expect(url).toContain("fmt=mp3");
    expect(url).toContain("title=My+Video");
  });
});

describe("fetchInfo timeout", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("aborts and throws a timeout error after FETCH_INFO_TIMEOUT_MS", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(new DOMException("aborted", "AbortError"));
          });
        }),
    );

    const promise = fetchInfo("https://www.youtube.com/watch?v=x");
    void vi.advanceTimersByTimeAsync(FETCH_INFO_TIMEOUT_MS);

    await expect(promise).rejects.toThrow(/timed out/i);
  });

  it("resolves and clears the timer on a successful response", async () => {
    const sampleBody = {
      video_id: "x",
      title: "T",
      thumbnail: "",
      duration: 0,
      uploader: "",
      formats: [],
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(sampleBody), { status: 200 }),
    );
    const clearSpy = vi.spyOn(globalThis, "clearTimeout");

    const result = await fetchInfo("https://www.youtube.com/watch?v=x");

    expect(result).toMatchObject({ video_id: "x" });
    expect(clearSpy).toHaveBeenCalled();
  });
});

describe("fetchInfo error bodies", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  function mockResponse(body: string, status: number, contentType: string) {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(body, { status, headers: { "content-type": contentType } }),
    );
  }

  it("surfaces the message from the API error envelope", async () => {
    mockResponse(
      JSON.stringify({ error: { code: "invalid_url", message: "Bad URL." } }),
      400,
      "application/json",
    );

    await expect(fetchInfo("https://www.youtube.com/watch?v=x")).rejects.toThrow(
      "Bad URL.",
    );
  });

  it("reports the status when the body is HTML rather than JSON", async () => {
    // An edge proxy answering 429 with its stock HTML page used to
    // surface as "Unexpected token '<' ... is not valid JSON".
    mockResponse("<html><body>429</body></html>", 429, "text/html");

    await expect(
      fetchInfo("https://www.youtube.com/watch?v=x"),
    ).rejects.toThrow("Request failed (HTTP 429).");
  });

  it("reports the status when the JSON has no error envelope", async () => {
    // FastAPI's own validation errors use {"detail": [...]}.
    mockResponse(JSON.stringify({ detail: [] }), 422, "application/json");

    await expect(
      fetchInfo("https://www.youtube.com/watch?v=x"),
    ).rejects.toThrow("Request failed (HTTP 422).");
  });
});
