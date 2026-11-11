/**
 * An unsigned JWT-shaped token for frontend tests: header, a payload with
 * `sub` and a serial `n`, and a fake signature. The client reads `sub` only to
 * tell one account's token from another's (`tokenSubject`), so this is enough.
 */
function base64Url(value: unknown): string {
  return btoa(JSON.stringify(value)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function fakeJwt(sub: string, n: number | string = 1): string {
  return `${base64Url({ alg: "HS256", typ: "JWT" })}.${base64Url({ sub, n })}.sig`;
}
