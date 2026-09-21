import {
  buildDownloadUrl,
  fetchInfo,
  isPlaylistInfo,
  isVideoInfo,
  type PlaylistInfo,
  type VideoInfo,
} from "./api";
import { createDownloadButton } from "./components/download-btn";
import {
  createFormatPicker,
  type FormatSelection,
} from "./components/format-picker";
import { createPlaylistView } from "./components/playlist-view";
import { createUrlInput, setUrlInputLoading } from "./components/url-input";
import { createVideoInfo } from "./components/video-info";
import "./styles/main.css";
import { escapeHtml } from "./utils";

interface Sections {
  url: HTMLElement;
  error: HTMLElement;
  info: HTMLElement;
  format: HTMLElement;
  download: HTMLElement;
}

interface SelectionStore {
  get(): FormatSelection;
  set(next: FormatSelection): void;
}

const DEFAULT_SELECTION: FormatSelection = { fmt: "mp4", quality: "best" };

function createSelectionStore(initial: FormatSelection): SelectionStore {
  let current = initial;
  return {
    get: () => current,
    set: (next) => {
      current = next;
    },
  };
}

const app = document.querySelector<HTMLDivElement>("#app")!;

function render(): void {
  app.innerHTML = `
    <main class="min-h-screen bg-gray-50 flex flex-col items-center px-4 py-12">
      <h1 class="text-3xl font-bold text-gray-900 mb-2">YouTube Downloader</h1>
      <p class="text-gray-500 mb-8">Privacy-first video downloader</p>
      <div id="url-section" class="w-full flex justify-center"></div>
      <div id="error-section" role="status" aria-live="polite"
           class="w-full flex justify-center mt-4"></div>
      <div id="info-section" class="w-full flex justify-center mt-4"></div>
      <div id="format-section" class="w-full flex justify-center mt-4"></div>
      <div id="download-section" class="w-full flex justify-center mt-4"></div>
    </main>
  `;

  const sections = getSections();
  sections.url.appendChild(createUrlInput((url) => handleUrlSubmit(url, sections)));
}

function getSections(): Sections {
  const find = (id: string) => app.querySelector<HTMLDivElement>(`#${id}`)!;
  return {
    url: find("url-section"),
    error: find("error-section"),
    info: find("info-section"),
    format: find("format-section"),
    download: find("download-section"),
  };
}

async function handleUrlSubmit(url: string, sections: Sections): Promise<void> {
  clearOutputSections(sections);
  setUrlInputLoading(sections.url, true);

  try {
    const info = await fetchInfo(url);
    const selection = createSelectionStore(DEFAULT_SELECTION);

    if (isVideoInfo(info)) {
      renderVideoFlow({ info, url, sections, selection });
    } else if (isPlaylistInfo(info)) {
      renderPlaylistFlow({ info, sections, selection });
    } else {
      // Neither guard matched, so the response is not a shape this
      // build knows. Without this branch the spinner just stopped and
      // left a blank page with nothing to act on.
      showError(
        sections.error,
        new Error("Unexpected response from the server. Please try again."),
      );
    }
  } catch (err) {
    showError(sections.error, err);
  } finally {
    setUrlInputLoading(sections.url, false);
  }
}

function clearOutputSections(sections: Sections): void {
  sections.error.innerHTML = "";
  sections.info.innerHTML = "";
  sections.format.innerHTML = "";
  sections.download.innerHTML = "";
}

function showError(target: HTMLElement, err: unknown): void {
  const message = err instanceof Error ? err.message : "An error occurred";
  target.innerHTML = `
    <div class="w-full max-w-2xl bg-red-50 border border-red-200 rounded-lg p-3">
      <p class="text-sm text-red-700">${escapeHtml(message)}</p>
    </div>
  `;
}

interface VideoFlowOptions {
  info: VideoInfo;
  url: string;
  sections: Sections;
  selection: SelectionStore;
}

function renderVideoFlow(opts: VideoFlowOptions): void {
  const { info, url, sections, selection } = opts;

  sections.info.appendChild(createVideoInfo(info));
  sections.format.appendChild(createFormatPicker(selection.set));

  sections.download.appendChild(
    createDownloadButton(() => {
      const sel = selection.get();
      const downloadUrl = buildDownloadUrl(url, sel.fmt, sel.quality, info.title);
      window.open(downloadUrl, "_blank", "noopener,noreferrer");
    }),
  );
}

interface PlaylistFlowOptions {
  info: PlaylistInfo;
  sections: Sections;
  selection: SelectionStore;
}

function renderPlaylistFlow(opts: PlaylistFlowOptions): void {
  const { info, sections, selection } = opts;

  sections.format.appendChild(createFormatPicker(selection.set));
  sections.info.appendChild(createPlaylistView(info, selection.get));
}

render();
