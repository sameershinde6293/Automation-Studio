/**
 * Execution history panel tests: listing, filtering, replay and resume.
 */

import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ExecutionHistoryPanel } from '../components/execution/ExecutionHistoryPanel';
import { useExecutionStore } from '../stores/executionStore';

const sampleHistory = [
  {
    id: 1,
    workflow_id: 3,
    workflow_name: 'Nightly Report',
    status: 'COMPLETED',
    trigger: 'manual',
    priority: 50,
    metrics: { duration_ms: 1500 },
  },
  {
    id: 2,
    workflow_id: 3,
    workflow_name: 'Nightly Report',
    status: 'FAILED',
    trigger: 'scheduled',
    priority: 50,
    error: 'HTTP 500 from upstream',
    metrics: {},
  },
];

beforeEach(() => {
  useExecutionStore.setState({
    history: [],
    historyTotal: 0,
    historyLoading: false,
    loadHistory: vi.fn().mockResolvedValue(undefined),
    loadExecution: vi.fn().mockResolvedValue(undefined),
    replay: vi.fn().mockResolvedValue(1),
    resumeFailed: vi.fn().mockResolvedValue(1),
  } as any);
});

describe('ExecutionHistoryPanel', () => {
  it('requests history on mount', async () => {
    render(<ExecutionHistoryPanel workflowId={3} />);
    await waitFor(() =>
      expect(useExecutionStore.getState().loadHistory).toHaveBeenCalled(),
    );
  });

  it('scopes the request to the given workflow', async () => {
    render(<ExecutionHistoryPanel workflowId={3} />);
    await waitFor(() => {
      expect(useExecutionStore.getState().loadHistory).toHaveBeenCalledWith(
        expect.objectContaining({ workflowId: 3 }),
      );
    });
  });

  it('shows an empty state', () => {
    render(<ExecutionHistoryPanel workflowId={3} />);
    expect(screen.getByTestId('history-empty')).toBeInTheDocument();
  });

  it('shows a loading state', () => {
    useExecutionStore.setState({ historyLoading: true } as any);
    render(<ExecutionHistoryPanel workflowId={3} />);
    expect(screen.getByTestId('history-loading')).toBeInTheDocument();
  });

  it('renders execution rows', () => {
    useExecutionStore.setState({ history: sampleHistory, historyTotal: 2 } as any);
    render(<ExecutionHistoryPanel workflowId={3} />);
    expect(screen.getAllByTestId('history-item')).toHaveLength(2);
    expect(screen.getByText(/#1 Nightly Report/)).toBeInTheDocument();
  });

  it('shows the error text for failed runs', () => {
    useExecutionStore.setState({ history: sampleHistory, historyTotal: 2 } as any);
    render(<ExecutionHistoryPanel workflowId={3} />);
    expect(screen.getByTestId('history-error')).toHaveTextContent('HTTP 500');
  });

  it('shows duration when metrics are present', () => {
    useExecutionStore.setState({ history: sampleHistory, historyTotal: 2 } as any);
    render(<ExecutionHistoryPanel workflowId={3} />);
    expect(screen.getByText(/1500ms/)).toBeInTheDocument();
  });

  it('reloads when the status filter changes', async () => {
    render(<ExecutionHistoryPanel workflowId={3} />);
    fireEvent.change(screen.getByTestId('history-filter'), { target: { value: 'FAILED' } });
    await waitFor(() => {
      expect(useExecutionStore.getState().loadHistory).toHaveBeenCalledWith(
        expect.objectContaining({ status: ['FAILED'] }),
      );
    });
  });

  it('omits the status filter when set to ALL', async () => {
    render(<ExecutionHistoryPanel workflowId={3} />);
    await waitFor(() => {
      expect(useExecutionStore.getState().loadHistory).toHaveBeenCalledWith(
        expect.objectContaining({ status: undefined }),
      );
    });
  });

  it('reloads when the search text changes', async () => {
    render(<ExecutionHistoryPanel workflowId={3} />);
    fireEvent.change(screen.getByTestId('history-search'), { target: { value: 'nightly' } });
    await waitFor(() => {
      expect(useExecutionStore.getState().loadHistory).toHaveBeenCalledWith(
        expect.objectContaining({ search: 'nightly' }),
      );
    });
  });

  it('opens an execution when its title is clicked', () => {
    useExecutionStore.setState({ history: sampleHistory, historyTotal: 2 } as any);
    render(<ExecutionHistoryPanel workflowId={3} />);
    fireEvent.click(screen.getAllByTestId('history-open')[0]);
    expect(useExecutionStore.getState().loadExecution).toHaveBeenCalledWith(1);
  });

  it('replays an execution', () => {
    useExecutionStore.setState({ history: sampleHistory, historyTotal: 2 } as any);
    render(<ExecutionHistoryPanel workflowId={3} />);
    fireEvent.click(screen.getAllByTestId('history-replay')[0]);
    expect(useExecutionStore.getState().replay).toHaveBeenCalledWith(1);
  });

  it('offers resume-failed only for failed runs', () => {
    useExecutionStore.setState({ history: sampleHistory, historyTotal: 2 } as any);
    render(<ExecutionHistoryPanel workflowId={3} />);
    const buttons = screen.getAllByTestId('history-resume-failed');
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    expect(useExecutionStore.getState().resumeFailed).toHaveBeenCalledWith(2);
  });

  it('shows the result counter', () => {
    useExecutionStore.setState({ history: sampleHistory, historyTotal: 17 } as any);
    render(<ExecutionHistoryPanel workflowId={3} />);
    expect(screen.getByText('Showing 2 of 17')).toBeInTheDocument();
  });
});
