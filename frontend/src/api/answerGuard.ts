import { AxiosError, type AxiosResponse } from "axios";

/**
 * The axios error code of a 2xx answer the caller cannot use as JSON (issue
 * #1320). `loadFailureDetail` in lib/errors.ts reads it as "the answer could
 * not be read", never as "the server did not answer".
 */
export const UNREADABLE_ANSWER_CODE = "ERR_UNREADABLE_ANSWER";

/** The fixed message of that error; no response text is ever put in it. */
export const UNREADABLE_ANSWER_MESSAGE = "The answer was not the JSON this request expects";

/** True for the error `rejectUnreadableAnswer` raises. */
export function isUnreadableAnswer(err: unknown): boolean {
  return err instanceof AxiosError && err.code === UNREADABLE_ANSWER_CODE;
}

/**
 * A response interceptor for every HERD JSON client (`apiClient`, the config
 * client). A request that expects JSON (no `responseType`, or `json`) and gets
 * a 2xx whose body it cannot use is rejected with UNREADABLE_ANSWER_CODE, so
 * the page shows a load error instead of reading the body as data:
 *
 * - a `text/html` content type (before issue #1320, an /api path whose service
 *   was stopped fell through to the web app, which answered its index.html
 *   with a 200);
 * - a non-empty string body without a JSON content type (axios hands back the
 *   raw text when the body does not parse).
 *
 * A request that asked for a `blob`, `text`, `arraybuffer`, or `stream` (the
 * exports, the driver download, the report CSV) passes untouched, as does a
 * 204 or an empty body, and a JSON content type with any parsed body.
 *
 * The rejection carries the request's config but no `response`, so a 401
 * refresh never fires for it and no response text reaches the UI.
 */
export function rejectUnreadableAnswer(response: AxiosResponse): AxiosResponse {
  const responseType = response.config?.responseType;
  if (responseType && responseType !== "json") return response;
  if (response.status === 204) return response;
  const contentType = String(response.headers?.["content-type"] ?? "").toLowerCase();
  const data: unknown = response.data;
  const html = contentType.startsWith("text/html");
  const unparsedText =
    typeof data === "string" && data.trim() !== "" && !contentType.includes("json");
  if (html || unparsedText) {
    throw new AxiosError(
      UNREADABLE_ANSWER_MESSAGE,
      UNREADABLE_ANSWER_CODE,
      response.config,
      response.request,
    );
  }
  return response;
}
