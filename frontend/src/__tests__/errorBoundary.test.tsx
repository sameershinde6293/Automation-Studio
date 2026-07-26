/**
 * M5: error boundary behaviour.
 *
 * React logs caught errors to console.error; that is silenced per-test so the
 * output stays readable while still asserting the boundary itself worked.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ErrorBoundary } from '../components/ErrorBoundary';

const Boom = ({ message = 'exploded' }: { message?: string }) => {
  throw new Error(message);
};

describe('ErrorBoundary', () => {
  let consoleError: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    consoleError.mockRestore();
  });

  it('renders children when nothing throws', () => {
    render(
      <ErrorBoundary>
        <p>all good</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText('all good')).toBeInTheDocument();
    expect(screen.queryByTestId('error-boundary')).not.toBeInTheDocument();
  });

  it('renders a fallback instead of unmounting the tree', () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByTestId('error-boundary')).toBeInTheDocument();
    expect(screen.getByTestId('error-boundary-message')).toHaveTextContent('exploded');
  });

  it('exposes the failure to assistive technology', () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    const alert = screen.getByRole('alert');
    expect(alert).toHaveAttribute('aria-live', 'assertive');
  });

  it('names the failing region so the user knows what broke', () => {
    render(
      <ErrorBoundary name="Workflow editor">
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByText(/Workflow editor failed to render/i)).toBeInTheDocument();
  });

  it('notifies the onError callback', () => {
    const onError = vi.fn();
    render(
      <ErrorBoundary onError={onError}>
        <Boom message="observed" />
      </ErrorBoundary>,
    );
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0]).toBeInstanceOf(Error);
    expect(onError.mock.calls[0][0].message).toBe('observed');
  });

  it('supports a custom fallback renderer', () => {
    render(
      <ErrorBoundary fallback={(error) => <p>custom: {error.message}</p>}>
        <Boom message="nope" />
      </ErrorBoundary>,
    );
    expect(screen.getByText('custom: nope')).toBeInTheDocument();
  });

  it('recovers when reset and the child stops throwing', () => {
    const Flaky = ({ shouldThrow }: { shouldThrow: boolean }) => {
      if (shouldThrow) throw new Error('transient');
      return <p>recovered</p>;
    };

    const { rerender } = render(
      <ErrorBoundary>
        <Flaky shouldThrow />
      </ErrorBoundary>,
    );
    expect(screen.getByTestId('error-boundary')).toBeInTheDocument();

    rerender(
      <ErrorBoundary>
        <Flaky shouldThrow={false} />
      </ErrorBoundary>,
    );
    fireEvent.click(screen.getByRole('button', { name: /try again/i }));
    expect(screen.getByText('recovered')).toBeInTheDocument();
  });

  it('isolates a failure to its own subtree', () => {
    render(
      <div>
        <ErrorBoundary name="Panel A">
          <Boom />
        </ErrorBoundary>
        <ErrorBoundary name="Panel B">
          <p>panel b still works</p>
        </ErrorBoundary>
      </div>,
    );
    expect(screen.getByText('panel b still works')).toBeInTheDocument();
    expect(screen.getAllByTestId('error-boundary')).toHaveLength(1);
  });
});
