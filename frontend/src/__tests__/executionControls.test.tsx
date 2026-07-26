/**
 * Execution control UI tests: button availability per status and dispatch.
 */

import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ExecutionControls } from '../components/execution/ExecutionControls';
import { useExecutionStore } from '../stores/executionStore';

const setStatus = (status: any, extra: Record<string, any> = {}) =>
  useExecutionStore.setState({ status, ...extra });

beforeEach(() => {
  useExecutionStore.setState({
    executionId: null,
    status: null,
    isBusy: false,
    error: null,
    nodeStates: {},
    logs: [],
  });
  vi.restoreAllMocks();
});

describe('ExecutionControls — availability', () => {
  it('enables Run when idle', () => {
    render(<ExecutionControls workflowId={1} />);
    expect(screen.getByTestId('execution-run')).not.toBeDisabled();
  });

  it('disables Run without a workflow id', () => {
    render(<ExecutionControls workflowId={null} />);
    expect(screen.getByTestId('execution-run')).toBeDisabled();
  });

  it('disables Run while an execution is active', () => {
    setStatus('RUNNING');
    render(<ExecutionControls workflowId={1} />);
    expect(screen.getByTestId('execution-run')).toBeDisabled();
  });

  it('enables Pause and Stop while running', () => {
    setStatus('RUNNING');
    render(<ExecutionControls workflowId={1} />);
    expect(screen.getByTestId('execution-pause')).not.toBeDisabled();
    expect(screen.getByTestId('execution-stop')).not.toBeDisabled();
  });

  it('swaps Pause for Resume when paused', () => {
    setStatus('PAUSED');
    render(<ExecutionControls workflowId={1} />);
    expect(screen.getByTestId('execution-resume')).toBeInTheDocument();
    expect(screen.queryByTestId('execution-pause')).not.toBeInTheDocument();
  });

  it('disables Pause and Stop once finished', () => {
    setStatus('COMPLETED');
    render(<ExecutionControls workflowId={1} />);
    expect(screen.getByTestId('execution-pause')).toBeDisabled();
    expect(screen.getByTestId('execution-stop')).toBeDisabled();
  });

  it('enables Replay only for a finished execution', () => {
    setStatus('COMPLETED', { executionId: 5 });
    render(<ExecutionControls workflowId={1} />);
    expect(screen.getByTestId('execution-replay')).not.toBeDisabled();
  });

  it('disables Replay while still running', () => {
    setStatus('RUNNING', { executionId: 5 });
    render(<ExecutionControls workflowId={1} />);
    expect(screen.getByTestId('execution-replay')).toBeDisabled();
  });

  it('disables everything actionable while busy', () => {
    setStatus('RUNNING', { isBusy: true });
    render(<ExecutionControls workflowId={1} />);
    expect(screen.getByTestId('execution-pause')).toBeDisabled();
    expect(screen.getByTestId('execution-stop')).toBeDisabled();
  });

  it('shows the current status badge', () => {
    setStatus('QUEUED');
    render(<ExecutionControls workflowId={1} />);
    expect(screen.getByTestId('execution-status')).toHaveTextContent('QUEUED');
  });

  it('hides the badge when there is no execution', () => {
    render(<ExecutionControls workflowId={1} />);
    expect(screen.queryByTestId('execution-status')).not.toBeInTheDocument();
  });
});

describe('ExecutionControls — dispatch', () => {
  it('Run calls the store action with the workflow id', async () => {
    const run = vi.fn().mockResolvedValue(1);
    useExecutionStore.setState({ run, status: null, isBusy: false } as any);
    render(<ExecutionControls workflowId={42} />);
    fireEvent.click(screen.getByTestId('execution-run'));
    await vi.waitFor(() => expect(run).toHaveBeenCalledWith(42));
  });

  it('Run awaits onBeforeRun (used to save the graph first)', async () => {
    const order: string[] = [];
    const onBeforeRun = vi.fn(async () => {
      order.push('save');
    });
    const run = vi.fn(async () => {
      order.push('run');
      return 1;
    });
    useExecutionStore.setState({ run } as any);
    render(<ExecutionControls workflowId={7} onBeforeRun={onBeforeRun} />);
    fireEvent.click(screen.getByTestId('execution-run'));
    await vi.waitFor(() => expect(order).toEqual(['save', 'run']));
  });

  it('Pause calls the pause action', () => {
    const pause = vi.fn();
    useExecutionStore.setState({ status: 'RUNNING', pause } as any);
    render(<ExecutionControls workflowId={1} />);
    fireEvent.click(screen.getByTestId('execution-pause'));
    expect(pause).toHaveBeenCalled();
  });

  it('Resume calls the resume action', () => {
    const resume = vi.fn();
    useExecutionStore.setState({ status: 'PAUSED', resume } as any);
    render(<ExecutionControls workflowId={1} />);
    fireEvent.click(screen.getByTestId('execution-resume'));
    expect(resume).toHaveBeenCalled();
  });

  it('Stop calls the stop action', () => {
    const stop = vi.fn();
    useExecutionStore.setState({ status: 'RUNNING', stop } as any);
    render(<ExecutionControls workflowId={1} />);
    fireEvent.click(screen.getByTestId('execution-stop'));
    expect(stop).toHaveBeenCalled();
  });

  it('Force cancel calls the cancel action', () => {
    const cancel = vi.fn();
    useExecutionStore.setState({ status: 'RUNNING', cancel } as any);
    render(<ExecutionControls workflowId={1} />);
    fireEvent.click(screen.getByTestId('execution-cancel'));
    expect(cancel).toHaveBeenCalled();
  });

  it('Replay passes the current execution id', () => {
    const replay = vi.fn();
    useExecutionStore.setState({ status: 'FAILED', executionId: 99, replay } as any);
    render(<ExecutionControls workflowId={1} />);
    fireEvent.click(screen.getByTestId('execution-replay'));
    expect(replay).toHaveBeenCalledWith(99);
  });

  it('does not dispatch Run when disabled', () => {
    const run = vi.fn();
    useExecutionStore.setState({ status: 'RUNNING', run } as any);
    render(<ExecutionControls workflowId={1} />);
    fireEvent.click(screen.getByTestId('execution-run'));
    expect(run).not.toHaveBeenCalled();
  });
});
