/**
 * Execution API client tests: request shapes, error envelope, graph adapter and
 * the SSE / polling stream.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ExecutionApiError,
  isActive,
  isTerminal,
  listExecutions,
  pauseExecution,
  runWorkflow,
  streamExecution,
} from '../api/executionApi';
import {
  deserializeGraph,
  resolveIdMap,
  serializeGraph,
} from '../stores/graphAdapter';
import { MockEventSource, installMockEventSource, removeEventSource } from '../test/setup';

const okFetch = (payload: any) =>
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: true, status: 200, json: async () => payload,
  }));

afterEach(() => {
  vi.restoreAllMocks();
  removeEventSource();
});

describe('status helpers', () => {
  it('classifies terminal statuses', () => {
    expect(isTerminal('COMPLETED')).toBe(true);
    expect(isTerminal('FAILED')).toBe(true);
    expect(isTerminal('CANCELLED')).toBe(true);
    expect(isTerminal('RUNNING')).toBe(false);
    expect(isTerminal(null)).toBe(false);
  });

  it('classifies active statuses', () => {
    expect(isActive('RUNNING')).toBe(true);
    expect(isActive('PAUSED')).toBe(true);
    expect(isActive('COMPLETED')).toBe(false);
  });
});

describe('request handling', () => {
  it('posts the expected run payload', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, status: 200, json: async () => ({ execution_id: 1, status: 'QUEUED' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    await runWorkflow(5, { priority: 10, inputData: { a: 1 } });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain('/workflows/5/executions');
    expect(init.method).toBe('POST');
    const body = JSON.parse(init.body);
    expect(body.priority).toBe(10);
    expect(body.input_data).toEqual({ a: 1 });
    expect(body.wait).toBe(false);
  });

  it('unwraps the backend error envelope', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({ error: { code: 'conflict', message: 'already finished' } }),
    }));

    await expect(pauseExecution(1)).rejects.toThrowError(ExecutionApiError);
    await expect(pauseExecution(1)).rejects.toThrow('already finished');
  });

  it('handles a non-JSON error body', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => { throw new Error('not json'); },
    }));
    await expect(pauseExecution(1)).rejects.toThrow('status 500');
  });

  it('builds history query strings', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, status: 200, json: async () => ({ items: [], total: 0, has_more: false }),
    });
    vi.stubGlobal('fetch', fetchMock);

    await listExecutions({ workflowId: 2, status: ['FAILED', 'COMPLETED'], search: 'x', limit: 10 });

    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain('workflow_id=2');
    expect(url).toContain('status=FAILED');
    expect(url).toContain('status=COMPLETED');
    expect(url).toContain('search=x');
    expect(url).toContain('limit=10');
  });
});

describe('graph adapter', () => {
  const nodes = [
    { id: 'uuid-a', type: 'start', position: { x: 10, y: 20 }, data: { label: 'Begin', config: {} } },
    { id: 'uuid-b', type: 'httpRequest', position: { x: 30, y: 40 }, data: { label: 'Fetch', config: { url: 'https://x' } } },
  ] as any[];

  it('converts editor nodes into the backend schema', () => {
    const { nodes: payload } = serializeGraph(nodes, []);
    expect(payload[0]).toMatchObject({
      id: 1, name: 'Begin', node_type: 'start', position_x: 10, position_y: 20,
    });
    expect(payload[1].config).toEqual({ url: 'https://x' });
  });

  it('maps string edge endpoints onto integer ids', () => {
    const edges = [{ id: 'e1', source: 'uuid-a', target: 'uuid-b' }] as any[];
    const { edges: payload } = serializeGraph(nodes, edges);
    expect(payload).toEqual([{ source_id: 1, target_id: 2, label: null }]);
  });

  it('carries the source handle through as the branch label', () => {
    const edges = [{ id: 'e1', source: 'uuid-a', target: 'uuid-b', sourceHandle: 'true' }] as any[];
    const { edges: payload } = serializeGraph(nodes, edges);
    expect(payload[0].label).toBe('true');
  });

  it('drops edges that reference unknown nodes', () => {
    const edges = [{ id: 'e1', source: 'uuid-a', target: 'ghost' }] as any[];
    const { edges: payload } = serializeGraph(nodes, edges);
    expect(payload).toHaveLength(0);
  });

  it('falls back to the node type when no label is set', () => {
    const unlabelled = [{ id: 'x', type: 'delay', position: { x: 0, y: 0 }, data: { config: {} } }] as any[];
    const { nodes: payload } = serializeGraph(unlabelled, []);
    expect(payload[0].name).toBe('delay');
  });

  it('joins ordinals to backend ids', () => {
    const { ordinalMap } = serializeGraph(nodes, []);
    const resolved = resolveIdMap(ordinalMap, { '1': 101, '2': 102 });
    expect(resolved).toEqual({ 'uuid-a': 101, 'uuid-b': 102 });
  });

  it('falls back to the ordinal when the backend omits a mapping', () => {
    const { ordinalMap } = serializeGraph(nodes, []);
    expect(resolveIdMap(ordinalMap, undefined)).toEqual({ 'uuid-a': 1, 'uuid-b': 2 });
  });

  it('deserializes a backend graph into editor shape', () => {
    const { nodes: editorNodes, edges, idMap } = deserializeGraph({
      nodes: [{ id: 7, name: 'Begin', node_type: 'start', config: {}, position_x: 5, position_y: 6 }],
      edges: [{ id: 3, source_id: 7, target_id: 8, label: 'false' }],
    });
    expect(editorNodes[0]).toMatchObject({ id: '7', type: 'start', position: { x: 5, y: 6 } });
    expect(editorNodes[0].data.label).toBe('Begin');
    expect(edges[0]).toMatchObject({ source: '7', target: '8', sourceHandle: 'false' });
    expect(idMap['7']).toBe(7);
  });

  it('round-trips a graph without losing structure', () => {
    const { nodes: payload, edges: payloadEdges, ordinalMap } = serializeGraph(
      nodes,
      [{ id: 'e', source: 'uuid-a', target: 'uuid-b' }] as any[],
    );
    const back = deserializeGraph({
      nodes: payload.map((n) => ({ ...n, id: ordinalMap[Object.keys(ordinalMap)[n.id - 1]] ?? n.id })),
      edges: payloadEdges.map((e, i) => ({ id: i, ...e })),
    });
    expect(back.nodes).toHaveLength(2);
    expect(back.edges).toHaveLength(1);
  });

  it('handles an empty graph', () => {
    expect(serializeGraph([], [])).toMatchObject({ nodes: [], edges: [] });
    expect(deserializeGraph({})).toMatchObject({ nodes: [], edges: [] });
  });
});

describe('streamExecution', () => {
  it('delivers SSE events to the callback', () => {
    installMockEventSource();
    const received: any[] = [];
    streamExecution(1, (event) => received.push(event));

    const source = MockEventSource.instances[0];
    source.emit('node.started', { execution_id: 1, node_id: 2, sequence: 1 });
    expect(received).toHaveLength(1);
    expect(received[0].event).toBe('node.started');
  });

  it('closes the stream once the execution finishes', () => {
    installMockEventSource();
    streamExecution(1, () => {});
    const source = MockEventSource.instances[0];
    source.emit('execution.finished', { execution_id: 1, status: 'COMPLETED', sequence: 9 });
    expect(source.readyState).toBe(2);
  });

  it('surfaces stream errors', () => {
    installMockEventSource();
    const onError = vi.fn();
    streamExecution(1, () => {}, onError);
    MockEventSource.instances[0].fail();
    expect(onError).toHaveBeenCalled();
  });

  it('ignores malformed frames instead of tearing down', () => {
    installMockEventSource();
    const onEvent = vi.fn();
    streamExecution(1, onEvent);
    const source = MockEventSource.instances[0];
    source.onmessage?.({ data: 'not-json' } as MessageEvent);
    expect(onEvent).not.toHaveBeenCalled();
  });

  it('close() releases the connection', () => {
    installMockEventSource();
    const handle = streamExecution(1, () => {});
    handle.close();
    expect(MockEventSource.instances[0].readyState).toBe(2);
  });

  it('falls back to polling when EventSource is unavailable', async () => {
    removeEventSource();
    okFetch({ events: [{ event: 'node.started', node_id: 1, sequence: 1 }], last_sequence: 1 });
    const received: any[] = [];
    const handle = streamExecution(1, (event) => received.push(event));

    await vi.waitFor(() => expect(received.length).toBeGreaterThan(0));
    handle.close();
    expect(received[0].event).toBe('node.started');
  });
});
