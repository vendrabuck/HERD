// Bottom padding for a page whose last controls sit at the bottom centre, where
// toasts appear (`<Toaster position="bottom-center" />` in App.tsx, issue #942).
// With it the page can scroll its last control clear of the toast area: one
// toast occupies about the bottom 82 px of the viewport and a stack of three
// about the bottom 208 px, measured at 1280x720 (issue #988). pb-56 is 224 px.
// Applied per page, never to AppLayout's content area, which would also change
// the full-height topology editor.
export const TOAST_CLEARANCE_CLASS = "pb-56";
