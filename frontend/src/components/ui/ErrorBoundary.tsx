import { Component, useState } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";

interface ErrorBoundaryProps {
  children: ReactNode;
  // What to render once a child has thrown; the full-screen last-resort
  // message when omitted.
  fallback?: ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
}

function reloadPage() {
  window.location.reload();
}

/**
 * Catches a render-time throw below it. The fallback is fixed text: the
 * thrown error's message never reaches the screen (it can carry server text
 * or internals), only the browser console (issue #1344).
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("ErrorBoundary caught:", error, info.componentStack);
  }

  render() {
    if (!this.state.hasError) return this.props.children;
    if (this.props.fallback !== undefined) return this.props.fallback;
    // The root boundary in App.tsx: the last resort when the layout itself
    // failed, so a reload is the only way back.
    return (
      <div role="alert" className="flex flex-col items-center justify-center min-h-screen bg-gray-50 p-8">
        <h1 className="text-xl font-bold text-gray-900 mb-2">Something went wrong</h1>
        <p className="text-sm text-gray-600 mb-4">
          HERD could not display this screen. Reload the page to try again.
        </p>
        <button
          type="button"
          onClick={reloadPage}
          className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
        >
          Reload page
        </button>
      </div>
    );
  }
}

function RouteErrorFallback({ onRetry, showTopologyLink }: {
  onRetry: () => void;
  showTopologyLink: boolean;
}) {
  return (
    <div className="h-full overflow-y-auto">
      <div role="alert" className="max-w-lg mx-auto mt-16 p-6 bg-white border border-gray-200 rounded-lg">
        <h2 className="text-lg font-semibold text-gray-900 mb-2">This page could not be displayed</h2>
        <p className="text-sm text-gray-600 mb-4">
          Something went wrong while showing this page. Try again, use the navigation above to
          open another page, or reload.
        </p>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={onRetry}
            className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
          >
            Try again
          </button>
          <button
            type="button"
            onClick={reloadPage}
            className="px-4 py-2 text-sm font-medium text-gray-700 border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors"
          >
            Reload page
          </button>
          {showTopologyLink && (
            <Link
              to="/topology"
              className="px-4 py-2 text-sm font-medium text-gray-700 border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors"
            >
              Go to Topology
            </Link>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * The per-route boundary inside AppLayout (issue #1344): a page that throws
 * while rendering is replaced by a fixed message while the header and
 * navigation stay. It is keyed on the location pathname plus a retry count:
 * navigating to another pathname mounts a fresh boundary (and a fresh page),
 * and the fallback's Try again bumps the count, which remounts the same
 * route. A query string or hash change keeps the key, so it neither resets
 * the boundary nor remounts the page. Go to Topology is offered only off
 * `/topology`, where following it would change nothing.
 */
export function RouteErrorBoundary({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  const [retries, setRetries] = useState(0);
  return (
    <ErrorBoundary
      key={pathname + "#" + retries}
      fallback={
        <RouteErrorFallback
          onRetry={() => setRetries((n) => n + 1)}
          showTopologyLink={pathname !== "/topology"}
        />
      }
    >
      {children}
    </ErrorBoundary>
  );
}
