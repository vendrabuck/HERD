/**
 * The config page's new-password bound (issue #1452, rule OPS-CONFIG-8). The
 * config service counts characters as code points (Python `len`), so the page
 * does too: an emoji outside the Basic Multilingual Plane is one character to
 * the server and two UTF-16 units to `String.length` and to the HTML
 * `minlength` and `maxlength` attributes, which is why the inputs carry
 * neither. The page check must equal the server's, never be stricter: every
 * other refusal (a NUL, a lone surrogate) is left to the server's 422.
 */
export const CONFIG_PASSWORD_MIN_CHARS = 8;
export const CONFIG_PASSWORD_MAX_CHARS = 32;

/**
 * The number of code points in `text`, the count Python's `len` gives: a
 * surrogate pair is one, a lone surrogate is one, and each combining mark is
 * its own.
 */
export function codePointLength(text: string): number {
  return Array.from(text).length;
}

/** True when `text` is inside the config service's 8 to 32 character bound. */
export function configPasswordLengthOk(text: string): boolean {
  const length = codePointLength(text);
  return length >= CONFIG_PASSWORD_MIN_CHARS && length <= CONFIG_PASSWORD_MAX_CHARS;
}
