/**
 * Execution store tests: live event handling, controls and state derivation.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useExecutionStore, toNodeStatus, MAX_LOG_LINES } from '../stores/executionStore';

const resetStore = () =>
  useExecutionStore.setState({
    executionId: null,
    workflowId: null,
    status: null,
    nodeStates: {},
    logs: [],
    progress: { completed: 0, failed: 0, skipped: 0, total: 0, percent: 0, running: [] },
    metrics: {},
    error: null,
    isBusy: false,
    history: [],
    historyTotal: 0,
    historyLoading: false,
    idMap: {},
  });

describe('executionStore — status mapping', () => {
  beforeEach(resetStore);

  it('maps backend statuses onto node vocabulary', () => {
    expect(toNodeStatus('RUNNING')).toBe('running');
    expect(toNodeStatus('COMPLETED')).toBe('completed');
    expect(toNodeStatus('FAILED')).toBe('failed');
    expect(toNodeStatus('SKIPPED')).toBe('skipped');
    expect(toNodeStatus('CANCELLED')).toBe('cancelled');
    expect(toNodeStatus('PENDING')).toBe('idle');
    expect(toNodeStatus(undefined)).toBe('idle');
  });
});

describe('executionStore — event application', () => {
  beforeEach(resetStore);

  it('sets QUEUED on execution.queued', () => {
    useExecutionStore.getState().applyEvent({
      execution_id: 1, event: 'execution.queued', sequence: 1, at: '',
    });
    expect(useExecutionStore.getState().status).toBe('QUEUED');
  });

  it('records total node count on execution.started', () => {
    useExecutionStore.getState().applyEvent({
      execution_id: 1, event: 'execution.started', sequence: 1, at: '', node_count: 5,
    });
    const state = useExecutionStore.getState();
    expect(state.status).toBe('RUNNING');
    expect(state.progress.total).toBe(5);
  });

  it('tracks a node through running -> completed', () => {
    const store = useExecutionStore.getState();
    store.applyEvent({
      execution_id: 1, event: 'node.started', sequence: 1, at: '',
      node_id: 7, node_name: 'Fetch',
    });
    expect(useExecutionStore.getState().nodeStates[7].status).toBe('running');

    store.applyEvent({
      execution_id: 1, event: 'node.finished', sequence: 2, at: '',
      node_id: 7, node_name: 'Fetch', status: 'COMPLETED', duration_ms: 42,
    });
    const node = useExecutionStore.getState().nodeStates[7];
    expect(node.status).toBe('completed');
    expect(node.durationMs).toBe(42);
  });

  it('records failure details including the error code', () => {
    useExecutionStore.getState().applyEvent({
      execution_id: 1, event: 'node.finished', sequence: 1, at: '',
      node_id: 3, status: 'FAILED', error: 'boom', error_code: 'runtime',
    });
    const node = useExecutionStore.getState().nodeStates[3];
    expect(node.status).toBe('failed');
    expect(node.error).toBe('boom');
    expect(node.errorCode).toBe('runtime');
  });

  it('marks a node running again on retry and records the attempt', () => {
    const store = useExecutionStore.getState();
    store.applyEvent({
      execution_id: 1, event: 'node.started', sequence: 1, at: '', node_id: 2,
    });
    store.applyEvent({
      execution_id: 1, event: 'node.retry', sequence: 2, at: '',
      node_id: 2, attempt: 2, error: 'transient',
    });
    const node = useExecutionStore.getState().nodeStates[2];
    expect(node.status).toBe('running');
    expect(node.attempt).toBe(2);
  });

  it('marks skipped nodes', () => {
    useExecutionStore.getState().applyEvent({
      execution_id: 1, event: 'node.skipped', sequence: 1, at: '', node_id: 9,
    });
    expect(useExecutionStore.getState().nodeStates[9].status).toBe('skipped');
  });

  it('updates progress counters', () => {
    useExecutionStore.getState().applyEvent({
      execution_id: 1, event: 'execution.progress', sequence: 1, at: '',
      completed: 2, failed: 1, skipped: 0, total: 4, percent: 75, running: [4],
    });
    const progress = useExecutionStore.getState().progress;
    expect(progress.completed).toBe(2);
    expect(progress.failed).toBe(1);
    expect(progress.percent).toBe(75);
    expect(progress.running).toEqual([4]);
  });

  it('tracks pause, resume and stopping transitions', () => {
    const store = useExecutionStore.getState();
    store.applyEvent({ execution_id: 1, event: 'execution.paused', sequence: 1, at: '' });
    expect(useExecutionStore.getState().status).toBe('PAUSED');

    store.applyEvent({ execution_id: 1, event: 'execution.resumed', sequence: 2, at: '' });
    expect(useExecutionStore.getState().status).toBe('RUNNING');

    store.applyEvent({ execution_id: 1, event: 'execution.stopping', sequence: 3, at: '' });
    expect(useExecutionStore.getState().status).toBe('STOPPING');
  });

  it('captures final status and metrics on finish', () => {
    useExecutionStore.getState().applyEvent({
      execution_id: 1, event: 'execution.finished', sequence: 1, at: '',
      status: 'COMPLETED', metrics: { duration_ms: 120, total_tokens: 30 },
    });
    const state = useExecutionStore.getState();
    expect(state.status).toBe('COMPLETED');
    expect(state.metrics.duration_ms).toBe(120);
  });

  it('appends streamed log events', () => {
    useExecutionStore.getState().applyEvent({
      execution_id: 1, event: 'log', sequence: 1, at: '',
      level: 'WARNING', message: 'slow node', log_sequence: 4,
    });
    const logs = useExecutionStore.getState().logs;
    expect(logs).toHaveLength(1);
    expect(logs[0].level).toBe('WARNING');
    expect(logs[0].message).toBe('slow node');
  });

  it('ignores unknown event types without throwing', () => {
    expect(() =>
      useExecutionStore.getState().applyEvent({
        execution_id: 1, event: 'totally.unknown', sequence: 1, at: '',
      }),
    ).not.toThrow();
  });

  it('caps the log buffer', () => {
    const store = useExecutionStore.getState();
    for (let i = 0; i < MAX_LOG_LINES + 50; i += 1) {
      store.appendLog({ id: i, sequence: i, level: 'INFO', message: `line ${i}` });
    }
    const logs = useExecutionStore.getState().logs;
    expect(logs.length).toBe(MAX_LOG_LINES);
    // Oldest entries are dropped, newest retained.
    expect(logs[logs.length - 1].message).toBe(`line ${MAX_LOG_LINES + 49}`);
  });
});

describe('executionStore — id mapping', () => {
  beforeEach(resetStore);

  it('resolves editor ids to backend ids and back', () => {
    const store = useExecutionStore.getState();
    store.setIdMap({ 'uuid-a': 10, 'uuid-b': 11 });
    expect(useExecutionStore.getState().backendIdFor('uuid-a')).toBe(10);
    expect(useExecutionStore.getState().editorIdFor(11)).toBe('uuid-b');
    expect(useExecutionStore.getState().backendIdFor('missing')).toBeUndefined();
  });

  it('reports node status by editor id', () => {
    const store = useExecutionStore.getState();
    store.setIdMap({ 'uuid-a': 10 });
    store.applyEvent({
      execution_id: 1, event: 'node.started', sequence: 1, at: '', node_id: 10,
    });
    expect(useExecutionStore.getState().statusForEditorNode('uuid-a')).toBe('running');
    expect(useExecutionStore.getState().statusForEditorNode('unknown')).toBe('idle');
  });
});

describe('executionStore — hydration', () => {
  beforeEach(resetStore);

  it('derives node states and progress from a fetched detail', () => {
    useExecutionStore.getState().hydrate({
      id: 5,
      workflow_id: 2,
      status: 'FAILED',
      priority: 50,
      metrics: { duration_ms: 10 },
      error: 'node failed',
      state: {},
      input_data: {},
      log_count: 3,
      is_running: false,
      is_paused: false,
      node_executions: [
        { node_id: 1, status: 'COMPLETED', retry_count: 0, duration_ms: 5, iteration: 0 },
        { node_id: 2, status: 'FAILED', retry_count: 2, error: 'x', error_code: 'runtime', iteration: 0 },
        { node_id: 3, status: 'SKIPPED', retry_count: 0, iteration: 0 },
      ],
    } as any);

    const state = useExecutionStore.getState();
    expect(state.executionId).toBe(5);
    expect(state.status).toBe('FAILED');
    expect(state.nodeStates[1].status).toBe('completed');
    expect(state.nodeStates[2].status).toBe('failed');
    expect(state.progress.completed).toBe(1);
    expect(state.progress.failed).toBe(1);
    expect(state.progress.skipped).toBe(1);
    expect(state.progress.percent).toBe(100);
  });
});

describe('executionStore — controls', () => {
  beforeEach(() => {
    resetStore();
    vi.restoreAllMocks();
  });

  const mockFetch = (payload: any, ok = true, status = 200) =>
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok,
        status,
        json: async () => payload,
      }),
    );

  it('run() stores the new execution id', async () => {
    mockFetch({ execution_id: 77, status: 'QUEUED' });
    const id = await useExecutionStore.getState().run(3);
    expect(id).toBe(77);
    expect(useExecutionStore.getState().executionId).toBe(77);
  });

  it('run() surfaces backend errors instead of throwing', async () => {
    mockFetch({ error: { message: 'graph invalid' } }, false, 422);
    const id = await useExecutionStore.getState().run(3);
    expect(id).toBeNull();
    expect(useExecutionStore.getState().error).toContain('graph invalid');
  });

  it('pause() sets an optimistic PAUSING status', async () => {
    mockFetch({ changed: true, message: 'ok' });
    useExecutionStore.setState({ executionId: 12 });
    await useExecutionStore.getState().pause();
    expect(useExecutionStore.getState().status).toBe('PAUSING');
  });

  it('resume() sets RUNNING', async () => {
    mockFetch({ changed: true, message: 'ok' });
    useExecutionStore.setState({ executionId: 12 });
    await useExecutionStore.getState().resume();
    expect(useExecutionStore.getState().status).toBe('RUNNING');
  });

  it('stop() sets STOPPING', async () => {
    mockFetch({ changed: true, message: 'ok' });
    useExecutionStore.setState({ executionId: 12 });
    await useExecutionStore.getState().stop();
    expect(useExecutionStore.getState().status).toBe('STOPPING');
  });

  it('controls no-op safely when no execution is active', async () => {
    const spy = vi.fn();
    vi.stubGlobal('fetch', spy);
    await useExecutionStore.getState().pause();
    await useExecutionStore.getState().resume();
    await useExecutionStore.getState().stop();
    expect(spy).not.toHaveBeenCalled();
  });

  it('records a 409 conflict as an error', async () => {
    mockFetch({ error: { message: 'already finished' } }, false, 409);
    useExecutionStore.setState({ executionId: 12 });
    await useExecutionStore.getState().pause();
    expect(useExecutionStore.getState().error).toContain('already finished');
  });

  it('loadHistory() populates the list', async () => {
    mockFetch({ items: [{ id: 1, workflow_id: 1, status: 'COMPLETED', priority: 50, metrics: {} }], total: 1, has_more: false });
    await useExecutionStore.getState().loadHistory();
    expect(useExecutionStore.getState().history).toHaveLength(1);
    expect(useExecutionStore.getState().historyTotal).toBe(1);
  });

  it('reset() clears live state', () => {
    useExecutionStore.setState({
      executionId: 1,
      status: 'RUNNING',
      logs: [{ id: 1, sequence: 1, level: 'INFO', message: 'x' }],
    });
    useExecutionStore.getState().reset();
    const state = useExecutionStore.getState();
    expect(state.executionId).toBeNull();
    expect(state.status).toBeNull();
    expect(state.logs).toHaveLength(0);
  });
});
