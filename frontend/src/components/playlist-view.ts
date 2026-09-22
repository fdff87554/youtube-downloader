/**
 * Playlist view component showing video list with individual download.
 */

import { buildDownloadUrl, formatDuration, type PlaylistInfo } from "../api";

export function createPlaylistView(
  info: PlaylistInfo,
  getSelection: () => { fmt: "mp4" | "mp3"; quality: string },
): HTMLElement {
  const container = document.createElement("div");
  container.className = "w-full max-w-2xl bg-white rounded-lg shadow p-4";

  // YouTube-supplied strings are assigned through DOM properties, never
  // interpolated into markup: a title containing a double quote would
  // escape the attribute it sits in.
  const header = document.createElement("div");
  header.className = "mb-4";

  const playlistTitle = document.createElement("h2");
  playlistTitle.className = "text-lg font-semibold text-gray-900";
  playlistTitle.textContent = info.title;

  const playlistUploader = document.createElement("p");
  playlistUploader.className = "text-sm text-gray-600 mt-1";
  playlistUploader.textContent = info.uploader;

  const playlistCount = document.createElement("p");
  playlistCount.className = "text-sm text-gray-500 mt-1";
  playlistCount.textContent = `${info.video_count} videos`;

  header.append(playlistTitle, playlistUploader, playlistCount);
  container.appendChild(header);

  const list = document.createElement("ul");
  list.className = "divide-y divide-gray-100";

  for (const entry of info.entries) {
    const item = document.createElement("li");
    item.className = "flex items-center gap-3 py-3";

    const thumb = document.createElement("img");
    thumb.src = entry.thumbnail;
    thumb.alt = entry.title;
    thumb.className = "w-24 h-auto rounded flex-shrink-0";
    thumb.loading = "lazy";

    const details = document.createElement("div");
    details.className = "flex-1 min-w-0";

    const entryTitle = document.createElement("p");
    entryTitle.className = "text-sm font-medium text-gray-800 truncate";
    entryTitle.title = entry.title;
    entryTitle.textContent = entry.title;

    const entryDuration = document.createElement("p");
    entryDuration.className = "text-xs text-gray-500";
    entryDuration.textContent = formatDuration(entry.duration);

    details.append(entryTitle, entryDuration);

    const downloadBtn = document.createElement("button");
    downloadBtn.className =
      "flex-shrink-0 px-3 py-1.5 text-sm bg-green-600 text-white rounded " +
      "hover:bg-green-700 transition-colors";
    downloadBtn.textContent = "Download";
    downloadBtn.addEventListener("click", () => {
      const sel = getSelection();
      const videoUrl = `https://www.youtube.com/watch?v=${entry.video_id}`;
      const url = buildDownloadUrl(videoUrl, sel.fmt, sel.quality, entry.title);
      window.open(url, "_blank", "noopener,noreferrer");
    });

    item.appendChild(thumb);
    item.appendChild(details);
    item.appendChild(downloadBtn);
    list.appendChild(item);
  }

  container.appendChild(list);
  return container;
}
