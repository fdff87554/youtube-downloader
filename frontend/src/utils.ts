/**
 * Shared utility functions.
 */

/**
 * Escape text for interpolation into a **text node**.
 *
 * This serialises through a detached element, which escapes `&`, `<`
 * and `>` but deliberately leaves quotes alone -- quotes need no
 * escaping between tags. Do not use the result inside an attribute
 * value: assign the raw string to the DOM property instead
 * (`el.title = value`), which needs no escaping at all.
 */
export function escapeHtml(text: string): string {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
