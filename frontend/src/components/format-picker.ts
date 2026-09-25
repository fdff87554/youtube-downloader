/**
 * Format and quality selection component.
 */

export interface FormatSelection {
  fmt: "mp4" | "mp3";
  quality: string;
}

export function createFormatPicker(
  onChange: (selection: FormatSelection) => void,
): HTMLElement {
  const container = document.createElement("div");
  container.className = "w-full max-w-2xl bg-white rounded-lg shadow p-4";

  container.innerHTML = `
    <div class="flex gap-6 items-center flex-wrap">
      <div>
        <label for="format-select"
               class="block text-sm font-medium text-gray-700 mb-1">Format</label>
        <select id="format-select"
                class="px-3 py-2 border border-gray-300 rounded-lg bg-white
                       focus:outline-none focus:ring-2 focus:ring-blue-500">
          <option value="mp4">MP4 (Video)</option>
          <option value="mp3">MP3 (Audio)</option>
        </select>
      </div>
      <div id="quality-group">
        <label for="quality-select"
               class="block text-sm font-medium text-gray-700 mb-1">Quality</label>
        <select id="quality-select"
                class="px-3 py-2 border border-gray-300 rounded-lg bg-white
                       focus:outline-none focus:ring-2 focus:ring-blue-500">
          <option value="best">Best (most compatible)</option>
          <option value="1080">Up to 1080p</option>
          <option value="720">Up to 720p</option>
          <option value="480">Up to 480p</option>
        </select>
      </div>
    </div>
  `;

  const formatSelect = container.querySelector<HTMLSelectElement>("#format-select")!;
  const qualitySelect = container.querySelector<HTMLSelectElement>("#quality-select")!;
  const qualityGroup = container.querySelector<HTMLDivElement>("#quality-group")!;

  function emitChange(): void {
    onChange({
      fmt: formatSelect.value as "mp4" | "mp3",
      quality: qualitySelect.value,
    });
  }

  formatSelect.addEventListener("change", () => {
    const isAudio = formatSelect.value === "mp3";
    qualityGroup.style.display = isAudio ? "none" : "";
    emitChange();
  });

  qualitySelect.addEventListener("change", emitChange);

  emitChange();

  return container;
}
