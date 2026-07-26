/**
 * Log viewer tests: rendering, level filtering, search and progress display.
 */

import React from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { LogViewer } from '../components/execution/LogViewer';
import { ExecutionProgressBar } from '../components/execution/ExecutionProgress';
import { useExecutionStore } from '../stores/executionStore';

const seedLogs = (logs: any[]) => useExecutionStore.setState({ logs });

beforeEach(() => {
  useExecutionStore.setState({
    logs: [],
    nodeStates: {},
    metrics: {},
    status: null,
    progress: { completed: 0, failed: 0, skipped: 0, total: 0, percent: 0, running: [] },
  });
});

describe('LogViewer', () => {
  it('shows an empty state with no logs', () => {
    render(<LogViewer />);
    expect(screen.getByTestId('log-empty')).toHaveTextContent('No logs yet');
  });

  it('renders log lines', () => {
    seedLogs([
      { id: 1, sequence: 1, level: 'INFO', message: 'started' },
      { id: 2, sequence: 2, level: 'ERROR', message: 'node failed' },
    ]);
    render(<LogViewer />);
    expect(screen.getAllByTestId('log-line')).toHaveLength(2);
    expect(screen.getByText('started')).toBeInTheDocument();
  });

  it('filters by level', () => {
    seedLogs([
      { id: 1, sequence: 1, level: 'INFO', message: 'info line' },
      { id: 2, sequence: 2, level: 'ERROR', message: 'error line' },
    ]);
    render(<LogViewer />);
    fireEvent.change(screen.getByTestId('log-level-filter'), { target: { value: 'ERROR' } });
    expect(screen.getAllByTestId('log-line')).toHaveLength(1);
    expect(screen.getByText('error line')).toBeInTheDocument();
  });

  it('filters by search text, case-insensitively', () => {
    seedLogs([
      { id: 1, sequence: 1, level: 'INFO', message: 'Fetching data' },
      { id: 2, sequence: 2, level: 'INFO', message: 'Writing file' },
    ]);
    render(<LogViewer />);
    fireEvent.change(screen.getByTestId('log-search'), { target: { value: 'fetch' } });
    expect(screen.getAllByTestId('log-line')).toHaveLength(1);
  });

  it('reports when a filter matches nothing', () => {
    seedLogs([{ id: 1, sequence: 1, level: 'INFO', message: 'hello' }]);
    render(<LogViewer />);
    fireEvent.change(screen.getByTestId('log-search'), { target: { value: 'zzz' } });
    expect(screen.getByTestId('log-empty')).toHaveTextContent('No logs match');
  });

  it('combines level and search filters', () => {
    seedLogs([
      { id: 1, sequence: 1, level: 'ERROR', message: 'timeout on node' },
      { id: 2, sequence: 2, level: 'INFO', message: 'timeout warning' },
    ]);
    render(<LogViewer />);
    fireEvent.change(screen.getByTestId('log-level-filter'), { target: { value: 'ERROR' } });
    fireEvent.change(screen.getByTestId('log-search'), { target: { value: 'timeout' } });
    expect(screen.getAllByTestId('log-line')).toHaveLength(1);
  });

  it('shows the filtered/total counter', () => {
    seedLogs([
      { id: 1, sequence: 1, level: 'INFO', message: 'a' },
      { id: 2, sequence: 2, level: 'ERROR', message: 'b' },
    ]);
    render(<LogViewer />);
    expect(screen.getByText('2 of 2 line(s)')).toBeInTheDocument();
  });

  it('exposes an auto-scroll toggle that is on by default', () => {
    render(<LogViewer />);
    expect(screen.getByTestId('log-autoscroll')).toBeChecked();
    fireEvent.click(screen.getByTestId('log-autoscroll'));
    expect(screen.getByTestId('log-autoscroll')).not.toBeChecked();
  });
});

describe('ExecutionProgressBar', () => {
  it('renders zero progress initially', () => {
    render(<ExecutionProgressBar />);
    expect(screen.getByTestId('progress-percent')).toHaveTextContent('0%');
  });

  it('reflects progress counters', () => {
    useExecutionStore.setState({
      progress: { completed: 3, failed: 1, skipped: 1, total: 5, percent: 100, running: [] },
    });
    render(<ExecutionProgressBar />);
    expect(screen.getByTestId('progress-percent')).toHaveTextContent('100%');
    expect(screen.getByTestId('progress-counts')).toHaveTextContent('3/5 done');
    expect(screen.getByTestId('progress-counts')).toHaveTextContent('1 failed');
  });

  it('exposes ARIA progressbar semantics', () => {
    useExecutionStore.setState({
      progress: { completed: 1, failed: 0, skipped: 0, total: 4, percent: 25, running: [] },
    });
    render(<ExecutionProgressBar />);
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '25');
  });

  it('names the currently running nodes', () => {
    useExecutionStore.setState({
      progress: { completed: 0, failed: 0, skipped: 0, total: 2, percent: 0, running: [1] },
      nodeStates: { 1: { nodeId: 1, nodeName: 'Fetch', status: 'running' } },
    });
    render(<ExecutionProgressBar />);
    expect(screen.getByTestId('running-nodes')).toHaveTextContent('Fetch');
  });

  it('falls back to the node id when no name is known', () => {
    useExecutionStore.setState({
      progress: { completed: 0, failed: 0, skipped: 0, total: 1, percent: 0, running: [8] },
      nodeStates: {},
    });
    render(<ExecutionProgressBar />);
    expect(screen.getByTestId('running-nodes')).toHaveTextContent('#8');
  });

  it('shows duration, token and cost metrics when present', () => {
    useExecutionStore.setState({
      metrics: { duration_ms: 1234, total_tokens: 500, cost_usd: 0.0125 },
    });
    render(<ExecutionProgressBar />);
    expect(screen.getByText(/1234ms/)).toBeInTheDocument();
    expect(screen.getByText(/500 tokens/)).toBeInTheDocument();
  });
});
