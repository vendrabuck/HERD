// issue #910: CSV formula-injection neutralization, the frontend counterpart
// of herd_common.csv_safety.csv_safe_cell. Backend CSV writers use that
// shared Python helper; the Reporting page builds its by-template CSV
// client-side, so it needs its own copy of the same rule. A value whose
// first character, ignoring any leading ASCII spaces, is a spreadsheet
// formula trigger (=, +, -, @, tab, or carriage return) opens as a formula
// rather than literal text when read by a spreadsheet application.
// Prefixing a single quote is the OWASP-recommended fix: every common
// spreadsheet app treats a leading `'` as a marker that the cell is literal
// text, and the quote itself is never shown to the user.
//
// Call this BEFORE any RFC 4180 comma/quote/newline escaping step (see
// ReportingPage.tsx's escapeCsvCell), so the neutralizing quote rides inside
// that escaping when the value also needs it, not outside it.
const CSV_FORMULA_TRIGGER = /^ *[=+\-@\t\r]/;

export function csvSafeCell(value: string): string {
  return CSV_FORMULA_TRIGGER.test(value) ? `'${value}` : value;
}
