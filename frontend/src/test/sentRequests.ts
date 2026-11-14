import { onTestFinished } from "vitest";

/**
 * Record every XMLHttpRequest the app sends, in the order it calls send().
 *
 * Why this exists (msw 3): in Node, msw 3 no longer replaces XMLHttpRequest.
 * jsdom's own XMLHttpRequest sends through its Undici dispatcher, and msw's
 * interceptors (0.42 and later) catch that traffic at the socket and parse the
 * raw HTTP. Two facts that msw 2 gave a handler for free are lost on that path:
 *
 * - Order. Requests in flight together travel on different sockets, so the
 *   handlers can run in a different order from the one the app sent them in.
 *   "The last request the page sent" can no longer be read from a handler's log.
 * - Credentials. `request.credentials` is derived from the XMLHttpRequest that
 *   opened the socket, sampled once per connection, so a later request on a
 *   kept-alive socket reports the first request's mode, not its own.
 *
 * msw 2 built the handler's Request from the XMLHttpRequest at send() time,
 * with `credentials` set from `withCredentials` ("include" or "same-origin").
 * This recorder reads the same facts at the same point, so a test that asserts
 * on it pins exactly what the msw 2 handler assertion pinned.
 *
 * Call it inside a test or a beforeEach. The patch is installed once per test,
 * shared by every recorder that test creates, and removed when the test
 * finishes. Requests sent through `fetch` are not seen, so a handler that
 * answers a recorded kind of request should check `wasSent(request.url)` and
 * fail when it is false: a change of transport then fails the test instead of
 * leaving the record silently short.
 */
export type SentRequest = {
  method: string;
  /** Absolute URL, resolved against the page the way the browser resolves it. */
  url: string;
  withCredentials: boolean;
};

export type SentRequests = SentRequest[] & {
  /** Whether a request to exactly this absolute URL was recorded. */
  wasSent: (url: string) => boolean;
};

type Listener = (request: SentRequest) => void;

let listeners: Listener[] | null = null;

function installForThisTest(): Listener[] {
  if (listeners) return listeners;
  const active: Listener[] = [];
  listeners = active;

  const opened = new WeakMap<XMLHttpRequest, { method: string; url: string }>();
  const proto = XMLHttpRequest.prototype;
  const realOpen = proto.open;
  const realSend = proto.send;

  proto.open = function open(this: XMLHttpRequest, ...args: unknown[]) {
    const [method, url] = args as [string, string | URL];
    opened.set(this, {
      method: method.toUpperCase(),
      url: new URL(String(url), document.baseURI).href,
    });
    return (realOpen as (...a: unknown[]) => void).apply(this, args);
  } as typeof proto.open;

  proto.send = function send(this: XMLHttpRequest, body?: Document | XMLHttpRequestBodyInit | null) {
    const target = opened.get(this);
    if (target) {
      const request = { ...target, withCredentials: this.withCredentials };
      for (const listener of active) listener(request);
    }
    return realSend.call(this, body);
  };

  onTestFinished(() => {
    proto.open = realOpen;
    proto.send = realSend;
    listeners = null;
  });
  return active;
}

/**
 * Start recording for the current test. `onSend` runs for each request at the
 * moment it is sent, for a caller that keeps its own view of the record.
 */
export function recordSentRequests(onSend?: Listener): SentRequests {
  const sent = [] as unknown as SentRequests;
  sent.wasSent = (url: string) => sent.some((r) => r.url === url);
  installForThisTest().push((request) => {
    sent.push(request);
    onSend?.(request);
  });
  return sent;
}
