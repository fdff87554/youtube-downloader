/**
 * URL input component with validation feedback.
 */

// Mirrors YOUTUBE_URL_PATTERN in backend/app/services/youtube.py,
// including the case-insensitive host. The backend remains the
// authority; this only drives the inline feedback.
const YOUTUBE_URL_REGEX =
  /^https?:\/\/(?:www\.|m\.)?(?:youtube\.com|youtu\.be)\//i;

export function createUrlInput(onSubmit: (url: string) => void): HTMLElement {
  const container = document.createElement("div");
  container.className = "w-full max-w-2xl";

  container.innerHTML = `
    <form id="url-form" class="flex gap-2">
      <label for="url-input" class="sr-only">YouTube URL</label>
      <input
        id="url-input"
        type="url"
        placeholder="https://www.youtube.com/watch?v=..."
        class="flex-1 px-4 py-3 border border-gray-300 rounded-lg
               focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent
               text-gray-800 bg-white"
        autocomplete="off"
        required
        aria-describedby="url-error"
        aria-invalid="false"
      />
      <button
        id="url-submit"
        type="submit"
        class="px-6 py-3 bg-blue-600 text-white rounded-lg font-medium
               hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed
               transition-colors"
        disabled
      >
        Get Info
      </button>
    </form>
    <p id="url-error" role="status" aria-live="polite"
       class="mt-2 text-sm text-red-600 hidden"></p>
  `;

  const input = container.querySelector<HTMLInputElement>("#url-input")!;
  const button = container.querySelector<HTMLButtonElement>("#url-submit")!;
  const form = container.querySelector<HTMLFormElement>("#url-form")!;
  const errorEl = container.querySelector<HTMLParagraphElement>("#url-error")!;

  input.addEventListener("input", () => {
    const value = input.value.trim();
    const isValid = YOUTUBE_URL_REGEX.test(value);
    button.disabled = !isValid;
    const showError = Boolean(value) && !isValid;
    if (showError) {
      errorEl.textContent = "Please enter a valid YouTube URL.";
      errorEl.classList.remove("hidden");
    } else {
      errorEl.textContent = "";
      errorEl.classList.add("hidden");
    }
    input.setAttribute("aria-invalid", showError ? "true" : "false");
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const value = input.value.trim();
    if (YOUTUBE_URL_REGEX.test(value)) {
      onSubmit(value);
    }
  });

  return container;
}

export function setUrlInputLoading(container: HTMLElement, loading: boolean): void {
  const button = container.querySelector<HTMLButtonElement>("#url-submit")!;
  const input = container.querySelector<HTMLInputElement>("#url-input")!;
  button.disabled = loading;
  input.disabled = loading;
  button.textContent = loading ? "Loading..." : "Get Info";
}
