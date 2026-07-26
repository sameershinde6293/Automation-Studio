/**
 * Error boundary (M5).
 *
 * Before M5 the app had no boundary at all: a render error anywhere in the
 * React Flow canvas, the execution panel or a node component unmounted the
 * whole tree and left the user staring at a blank white window with no way to
 * recover short of reloading.
 *
 * This catches render/lifecycle errors, shows an actionable message, and
 * offers a retry that remounts the subtree rather than reloading the page — so
 * an unsaved workflow elsewhere in the app survives.
 *
 * Known React limitation: error boundaries do NOT catch errors thrown in event
 * handlers, async callbacks or effects that reject after unmount. Those paths
 * handle their own failures (see executionStore's error state).
 */

import React from 'react';

export interface ErrorBoundaryProps {
  children: React.ReactNode;
  /** Shown instead of the default panel. Receives the error and a reset fn. */
  fallback?: (error: Error, reset: () => void) => React.ReactNode;
  /** Human-readable name of the region, used in the default message. */
  name?: string;
  /** Notified on every caught error, for logging or telemetry. */
  onError?: (error: Error, info: React.ErrorInfo) => void;
}

interface ErrorBoundaryState {
  error: Error | null;
}

export class ErrorBoundary extends React.Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    this.props.onError?.(error, info);
    // Kept as console.error rather than a silent swallow: without this the
    // stack is lost entirely, which makes production reports unactionable.
    console.error(
      `[ErrorBoundary${this.props.name ? ` ${this.props.name}` : ''}]`,
      error,
      info.componentStack,
    );
  }

  reset = () => {
    this.setState({ error: null });
  };

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    if (this.props.fallback) return this.props.fallback(error, this.reset);

    return (
      <div
        role="alert"
        aria-live="assertive"
        data-testid="error-boundary"
        style={{
          padding: 24,
          margin: 16,
          border: '1px solid #7f1d1d',
          borderRadius: 8,
          background: '#1f0a0a',
          color: '#fca5a5',
          fontFamily: 'system-ui, sans-serif',
        }}
      >
        <h2 style={{ margin: '0 0 8px', fontSize: 18, color: '#fecaca' }}>
          {this.props.name ? `${this.props.name} failed to render` : 'Something went wrong'}
        </h2>
        <p style={{ margin: '0 0 12px', fontSize: 14 }}>
          The rest of the application is still running. You can retry this
          section without losing work elsewhere.
        </p>
        <pre
          data-testid="error-boundary-message"
          style={{
            margin: '0 0 12px',
            padding: 8,
            background: '#111',
            borderRadius: 4,
            fontSize: 12,
            overflowX: 'auto',
            whiteSpace: 'pre-wrap',
          }}
        >
          {error.message}
        </pre>
        <button
          type="button"
          onClick={this.reset}
          style={{
            padding: '6px 14px',
            borderRadius: 6,
            border: '1px solid #7f1d1d',
            background: '#450a0a',
            color: '#fecaca',
            cursor: 'pointer',
          }}
        >
          Try again
        </button>
      </div>
    );
  }
}

export default ErrorBoundary;
