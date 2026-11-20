// External links the app points at, in one place so they cannot drift.

// The published user manual (built from docs/manual/ on main). The header's
// help icon opens it (issue #960); README.md links the same address.
export const MANUAL_URL = "https://vendrabuck.github.io/HERD/manual/";

// The author credit (AuthorCredit, OPS-CREDIT-1): the sign-in, registration,
// and config sign-in and change-password pages show it under their card, and
// the About page shows it in its Frontend card. It renders as the prefix, a
// space, then the name as a link to the URL. A change here changes every
// rendered surface. The unit and e2e tests pin the text and URL on purpose, so
// they change with it, and README.md's Author section states them separately.
export const AUTHOR_CREDIT_PREFIX = "Designed and built by";
export const AUTHOR_NAME = "Lane Nelms";
export const AUTHOR_LINKEDIN_URL = "https://www.linkedin.com/in/lane-nelms/";
