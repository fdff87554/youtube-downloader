/**
 * API client for communicating with the YouTube Downloader backend.
 */

const API_BASE = "/api";

// Aligned with backend SOCKET_TIMEOUT in app/services/youtube.py.
export const FETCH_INFO_TIMEOUT_MS = 30_000;

export interface VideoFormat {
  format_id: string;
  ext: string;
  quality: string;
  has_video: boolean;
  has_audio: boolean;
  filesize_approx: number | null;
}

export interface VideoInfo {
  video_id: string;
  title: string;
  thumbnail: string;
  duration: number;
  uploader: string;
  formats: VideoFormat[];
}

export interface PlaylistEntry {
  video_id: string;
  title: string;
  duration: number;
  thumbnail: string;
}

export interface PlaylistInfo {
  playlist_id: string;
  title: string;
  uploader: string;
  video_count: number;
  entries: PlaylistEntry[];
}

export interface ApiError {
  error: {
    code: string;
    message: string;
  };
}

export type InfoResponse = VideoInfo | PlaylistInfo;

// These take `unknown` rather than InfoResponse on purpose: they run
// against whatever the network actually returned. `in` throws on null
// and on primitives, so with the narrower type the guard itself was
// the thing that failed on an unexpected body, and the caller's
// friendly "unrecognised response" branch was unreachable.
function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function isPlaylistInfo(info: unknown): info is PlaylistInfo {
  return isObject(info) && "playlist_id" in info;
}

export function isVideoInfo(info: unknown): info is VideoInfo {
  return isObject(info) && "video_id" in info && !("playlist_id" in info);
}

async function handleResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    // Not every error reaches us as the API envelope: an edge proxy or
    // a network appliance can answer with HTML, and FastAPI's own
    // validation errors use {"detail": ...}. Parsing either of those
    // blindly threw a SyntaxError or TypeError whose text ended up in
    // front of the user instead of something actionable.
    const body = await response.json().catch(() => null);
    const message = (body as ApiError | null)?.error?.message;
    throw new Error(message ?? `Request failed (HTTP ${response.status}).`);
  }
  return response.json();
}

export async function fetchInfo(url: string): Promise<InfoResponse> {
  const controller = new AbortController();
  const timeoutId = setTimeout(
    () => controller.abort(),
    FETCH_INFO_TIMEOUT_MS,
  );
  try {
    const response = await fetch(
      `${API_BASE}/info?${new URLSearchParams({ url })}`,
      { signal: controller.signal },
    );
    return await handleResponse<InfoResponse>(response);
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      throw new Error(
        `Request timed out after ${FETCH_INFO_TIMEOUT_MS / 1000}s. Please try again.`,
      );
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
}

export function buildDownloadUrl(
  url: string,
  fmt: "mp4" | "mp3" = "mp4",
  quality: string = "best",
  title?: string,
): string {
  const params = new URLSearchParams({ url, fmt, quality });
  if (title) {
    params.set("title", title);
  }
  return `${API_BASE}/download?${params}`;
}

export function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) {
    return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  return `${m}:${String(s).padStart(2, "0")}`;
}
