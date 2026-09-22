/**
 * Video information display component.
 */

import { formatDuration, type VideoInfo } from "../api";

export function createVideoInfo(info: VideoInfo): HTMLElement {
  const container = document.createElement("div");
  container.className =
    "w-full max-w-2xl bg-white rounded-lg shadow p-4 flex gap-4";

  // Every value below comes from YouTube metadata, so it is assigned
  // through DOM properties rather than interpolated into markup: a
  // title containing a double quote would otherwise close the
  // attribute it sits in and inject one of its own.
  const thumbnail = document.createElement("img");
  thumbnail.src = info.thumbnail;
  thumbnail.alt = info.title;
  thumbnail.className = "w-48 h-auto rounded object-cover flex-shrink-0";
  thumbnail.loading = "lazy";

  const details = document.createElement("div");
  details.className = "flex flex-col justify-center min-w-0";

  const title = document.createElement("h2");
  title.className = "text-lg font-semibold text-gray-900 truncate";
  title.title = info.title;
  title.textContent = info.title;

  const uploader = document.createElement("p");
  uploader.className = "text-sm text-gray-600 mt-1";
  uploader.textContent = info.uploader;

  const duration = document.createElement("p");
  duration.className = "text-sm text-gray-500 mt-1";
  duration.textContent = formatDuration(info.duration);

  details.append(title, uploader, duration);
  container.append(thumbnail, details);

  return container;
}
