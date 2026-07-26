/**
 * M5: application shell — accessibility, keyboard navigation, health state
 * and the fact that the real workflow editor is actually mounted.
 *
 * The M5 audit found the shell rendered the string "DAG Execution &
 * Orchestration active." for the Workflows tab: the M3/M4 editor existed but
 * was unreachable from the running app.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import App from '../App';

// The editor pulls in React Flow, which needs a real layout engine. The shell
// tests care that it is mounted, not how it paints.
vi.mock('../components/WorkflowEditor', () => ({
  WorkflowEditor: () => <div data-testid="workflow-editor">workflow editor</div>,
}));

const healthResponse = (status = 'healthy') =>
  Promise.resolve({ ok: true, json: () => Promise.resolve({ status }) } as Response);

describe('App shell', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(() => healthResponse()));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  describe('workflow editor mounting', () => {
    it('renders the real editor on the default tab', async () => {
      render(<App />);
      expect(await screen.findByTestId('workflow-editor')).toBeInTheDocument();
    });

    it('no longer shows the M4 placeholder text', () => {
      render(<App />);
      expect(screen.queryByText(/DAG Execution & Orchestration active/i)).toBeNull();
    });
  });

  describe('accessibility', () => {
    it('exposes tabs with the ARIA tabs pattern', () => {
      render(<App />);
      expect(screen.getByRole('tablist', { name: /main sections/i })).toBeInTheDocument();
      expect(screen.getAllByRole('tab')).toHaveLength(5);
    });

    it('marks exactly one tab selected', () => {
      render(<App />);
      const selected = screen.getAllByRole('tab').filter(
        (tab) => tab.getAttribute('aria-selected') === 'true',
      );
      expect(selected).toHaveLength(1);
      expect(selected[0]).toHaveTextContent('Workflows');
    });

    it('links each tab to its panel', () => {
      render(<App />);
      const tab = screen.getByRole('tab', { name: 'Workflows' });
      const panel = screen.getByRole('tabpanel');
      expect(tab).toHaveAttribute('aria-controls', panel.id);
      expect(panel).toHaveAttribute('aria-labelledby', tab.id);
    });

    it('keeps only the active tab in the tab order (roving tabindex)', () => {
      render(<App />);
      const tabs = screen.getAllByRole('tab');
      expect(tabs.filter((t) => t.getAttribute('tabindex') === '0')).toHaveLength(1);
    });

    it('announces backend status politely', () => {
      render(<App />);
      expect(screen.getByTestId('health-status')).toHaveAttribute('aria-live', 'polite');
    });
  });

  describe('keyboard navigation', () => {
    it('moves to the next tab with ArrowRight', () => {
      render(<App />);
      fireEvent.keyDown(screen.getByRole('tab', { name: 'Workflows' }), { key: 'ArrowRight' });
      expect(screen.getByRole('tab', { name: 'AI' })).toHaveAttribute('aria-selected', 'true');
    });

    it('wraps from the last tab to the first', () => {
      render(<App />);
      fireEvent.click(screen.getByRole('tab', { name: 'Automation' }));
      fireEvent.keyDown(screen.getByRole('tab', { name: 'Automation' }), { key: 'ArrowRight' });
      expect(screen.getByRole('tab', { name: 'Workflows' })).toHaveAttribute(
        'aria-selected',
        'true',
      );
    });

    it('wraps backwards from the first tab to the last', () => {
      render(<App />);
      fireEvent.keyDown(screen.getByRole('tab', { name: 'Workflows' }), { key: 'ArrowLeft' });
      expect(screen.getByRole('tab', { name: 'Automation' })).toHaveAttribute(
        'aria-selected',
        'true',
      );
    });

    it('jumps to the first and last tabs with Home and End', () => {
      render(<App />);
      fireEvent.keyDown(screen.getByRole('tab', { name: 'Workflows' }), { key: 'End' });
      expect(screen.getByRole('tab', { name: 'Automation' })).toHaveAttribute(
        'aria-selected',
        'true',
      );
      fireEvent.keyDown(screen.getByRole('tab', { name: 'Automation' }), { key: 'Home' });
      expect(screen.getByRole('tab', { name: 'Workflows' })).toHaveAttribute(
        'aria-selected',
        'true',
      );
    });

    it('ignores unrelated keys', () => {
      render(<App />);
      fireEvent.keyDown(screen.getByRole('tab', { name: 'Workflows' }), { key: 'a' });
      expect(screen.getByRole('tab', { name: 'Workflows' })).toHaveAttribute(
        'aria-selected',
        'true',
      );
    });
  });

  describe('backend health', () => {
    it('reports online when the backend is healthy', async () => {
      render(<App />);
      await waitFor(() =>
        expect(screen.getByTestId('health-status')).toHaveTextContent('Online'),
      );
    });

    it('reports offline and offers a retry when the fetch fails', async () => {
      vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('network down'))));
      render(<App />);
      await waitFor(() =>
        expect(screen.getByTestId('health-status')).toHaveTextContent(/offline/i),
      );
      expect(screen.getByTestId('health-retry')).toBeInTheDocument();
    });

    it('re-checks when retry is pressed', async () => {
      const fetchMock = vi
        .fn()
        .mockRejectedValueOnce(new Error('network down'))
        .mockImplementation(() => healthResponse());
      vi.stubGlobal('fetch', fetchMock);

      render(<App />);
      await waitFor(() => expect(screen.getByTestId('health-retry')).toBeInTheDocument());
      fireEvent.click(screen.getByTestId('health-retry'));
      await waitFor(() =>
        expect(screen.getByTestId('health-status')).toHaveTextContent('Online'),
      );
    });

    it('treats a non-healthy payload as offline', async () => {
      vi.stubGlobal('fetch', vi.fn(() => healthResponse('degraded')));
      render(<App />);
      await waitFor(() =>
        expect(screen.getByTestId('health-status')).toHaveTextContent(/offline/i),
      );
    });

    it('aborts the in-flight request on unmount', () => {
      const abort = vi.fn();
      vi.stubGlobal(
        'AbortController',
        class {
          signal = {} as AbortSignal;
          abort = abort;
        },
      );
      const { unmount } = render(<App />);
      unmount();
      expect(abort).toHaveBeenCalled();
    });

    it('does not set state after unmount', async () => {
      // A late resolve must not warn; the guard is the `mounted` ref.
      let resolve: (value: unknown) => void = () => {};
      vi.stubGlobal(
        'fetch',
        vi.fn(() => new Promise((r) => {
          resolve = r;
        })),
      );
      const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
      const { unmount } = render(<App />);
      unmount();
      resolve({ ok: true, json: () => Promise.resolve({ status: 'healthy' }) });
      await Promise.resolve();
      expect(errorSpy).not.toHaveBeenCalledWith(
        expect.stringContaining('unmounted component'),
      );
      errorSpy.mockRestore();
    });
  });

  describe('tab switching', () => {
    it('shows the enterprise panel when selected', () => {
      render(<App />);
      fireEvent.click(screen.getByRole('tab', { name: 'Enterprise' }));
      expect(screen.getByText(/Enterprise Features/i)).toBeInTheDocument();
      expect(screen.queryByTestId('workflow-editor')).toBeNull();
    });

    it('returns to the editor when Workflows is reselected', () => {
      render(<App />);
      fireEvent.click(screen.getByRole('tab', { name: 'AI' }));
      fireEvent.click(screen.getByRole('tab', { name: 'Workflows' }));
      expect(screen.getByTestId('workflow-editor')).toBeInTheDocument();
    });
  });
});
