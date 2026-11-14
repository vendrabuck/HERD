/**
 * Leave the single-page app for `url` with a full top-level navigation (the
 * single sign-on begin route, ADR 0016). Its own module so tests can replace
 * it: jsdom does not implement navigation.
 */
export function navigateTopLevel(url: string): void {
  window.location.assign(url);
}
