/**
 * Live execution state for the workflow editor.
 *
 * Replaces the M3 ExecutionPanel placeholder (which ran an empty setInterval
 * and simulated nothing) with real state driven by the backend SSE stream.
 *
 * Node status is keyed by the backend's **numeric** node id. The editor uses
 * client-side string ids, so the store keeps the id map produced by the last
 * graph save and exposes lookups in both directions.
 */

import { create } from 'zustand';
// Types are erased at build time, so they must be imported with `import type`.
// Mixing them into the value import made Rollup warn that these names are not
// exported by the module (they exist only in the type namespace).
import type {
  ExecutionDetail,
  ExecutionEvent,
  ExecutionStatus,
  ExecutionSummary,
  LogRecord,
  StreamHandle,
} from '../api/executionApi';
import {
  cancelExecution,
  getExecution,
  getLogs,
  isTerminal,
  listExecutions,
  pauseExecution,
  replayExecution,
  resumeExecution,
  resumeFailedExecution,
  runWorkflow,
  stopExecution,
  streamExecution,
} from '../api/executionApi';

export type NodeRunStatus =
  | 'idle'
  | 'running'
  | 'completed'
  | 'failed'
  | 'skipped'
  | 'cancelled';

export interface NodeRunState {
  nodeId: number;
  nodeName?: string;
  status: NodeRunStatus;
  durationMs?: number;
  error?: string;
  errorCode?: string;
  attempt?: number;
  iteration?: number;
}

export interface ExecutionProgress {
  completed: number;
  failed: number;
  skipped: number;
  total: number;
  percent: number;
  running: number[];
}

/** Cap the in-memory log buffer so a long run cannot grow without bound. */
export const MAX_LOG_LINES = 1000;

interface ExecutionStore {
  executionId: number | null;
  workflowId: number | null;
  status: ExecutionStatus | null;
  nodeStates: Record<number, NodeRunState>;
  logs: LogRecord[];
  progress: ExecutionProgress;
  metrics: Record<string, any>;
  error: string | null;
  isBusy: boolean;
  history: ExecutionSummary[];
  historyTotal: number;
  historyLoading: boolean;
  /** Editor node id (string) -> backend node id (number). */
  idMap: Record<string, number>;

  setIdMap: (map: Record<string, number>) => void;
  backendIdFor: (editorId: string) => number | undefined;
  editorIdFor: (backendId: number) => string | undefined;
  statusForEditorNode: (editorId: string) => NodeRunStatus;

  run: (workflowId: number | string, options?: Record<string, any>) => Promise<number | null>;
  pause: () => Promise<void>;
  resume: () => Promise<void>;
  stop: () => Promise<void>;
  cancel: () => Promise<void>;
  replay: (executionId: number) => Promise<number | null>;
  resumeFailed: (executionId: number) => Promise<number | null>;

  attach: (executionId: number) => void;
  detach: () => void;
  applyEvent: (event: ExecutionEvent) => void;
  hydrate: (detail: ExecutionDetail) => void;
  loadExecution: (executionId: number) => Promise<void>;
  loadLogs: (executionId: number) => Promise<void>;
  loadHistory: (query?: Record<string, any>) => Promise<void>;
  appendLog: (log: LogRecord) => void;
  reset: () => void;
}

const EMPTY_PROGRESS: ExecutionProgress = {
  completed: 0,
  failed: 0,
  skipped: 0,
  total: 0,
  percent: 0,
  running: [],
};

/** Map a backend ExecutionStatus onto the UI's per-node vocabulary. */
export function toNodeStatus(status?: string | null): NodeRunStatus {
  switch (status) {
    case 'RUNNING':
      return 'running';
    case 'COMPLETED':
      return 'completed';
    case 'FAILED':
      return 'failed';
    case 'SKIPPED':
      return 'skipped';
    case 'CANCELLED':
      return 'cancelled';
    default:
      return 'idle';
  }
}

// Stream handle lives outside the store: it is not serialisable state and must
// never trigger a re-render.
let activeStream: StreamHandle | null = null;

export const useExecutionStore = create<ExecutionStore>((set, get) => ({
  executionId: null,
  workflowId: null,
  status: null,
  nodeStates: {},
  logs: [],
  progress: { ...EMPTY_PROGRESS },
  metrics: {},
  error: null,
  isBusy: false,
  history: [],
  historyTotal: 0,
  historyLoading: false,
  idMap: {},

  setIdMap: (map) => set({ idMap: map }),

  backendIdFor: (editorId) => get().idMap[editorId],

  editorIdFor: (backendId) =>
    Object.keys(get().idMap).find((key) => get().idMap[key] === backendId),

  statusForEditorNode: (editorId) => {
    const backendId = get().idMap[editorId];
    if (backendId === undefined) return 'idle';
    return get().nodeStates[backendId]?.status ?? 'idle';
  },

  // ---------------------------------------------------------------- controls
  run: async (workflowId, options = {}) => {
    set({ isBusy: true, error: null });
    try {
      get().reset();
      const response = await runWorkflow(workflowId, options);
      const executionId = response.execution_id;
      set({
        executionId,
        workflowId: Number(workflowId),
        status: (response.status as ExecutionStatus) ?? 'PENDING',
        isBusy: false,
      });
      get().attach(executionId);
      return executionId;
    } catch (error) {
      set({ error: (error as Error).message, isBusy: false });
      return null;
    }
  },

  pause: async () => {
    const { executionId } = get();
    if (!executionId) return;
    set({ isBusy: true });
    try {
      await pauseExecution(executionId);
      set({ status: 'PAUSING' });
    } catch (error) {
      set({ error: (error as Error).message });
    } finally {
      set({ isBusy: false });
    }
  },

  resume: async () => {
    const { executionId } = get();
    if (!executionId) return;
    set({ isBusy: true });
    try {
      await resumeExecution(executionId);
      set({ status: 'RUNNING' });
    } catch (error) {
      set({ error: (error as Error).message });
    } finally {
      set({ isBusy: false });
    }
  },

  stop: async () => {
    const { executionId } = get();
    if (!executionId) return;
    set({ isBusy: true });
    try {
      await stopExecution(executionId);
      set({ status: 'STOPPING' });
    } catch (error) {
      set({ error: (error as Error).message });
    } finally {
      set({ isBusy: false });
    }
  },

  cancel: async () => {
    const { executionId } = get();
    if (!executionId) return;
    set({ isBusy: true });
    try {
      await cancelExecution(executionId);
    } catch (error) {
      set({ error: (error as Error).message });
    } finally {
      set({ isBusy: false });
    }
  },

  replay: async (executionId) => {
    try {
      const response = await replayExecution(executionId);
      get().reset();
      set({ executionId: response.execution_id, status: 'QUEUED' });
      get().attach(response.execution_id);
      return response.execution_id;
    } catch (error) {
      set({ error: (error as Error).message });
      return null;
    }
  },

  resumeFailed: async (executionId) => {
    try {
      const response = await resumeFailedExecution(executionId);
      get().reset();
      set({ executionId: response.execution_id, status: 'QUEUED' });
      get().attach(response.execution_id);
      return response.execution_id;
    } catch (error) {
      set({ error: (error as Error).message });
      return null;
    }
  },

  // ---------------------------------------------------------------- streaming
  attach: (executionId) => {
    get().detach();
    activeStream = streamExecution(
      executionId,
      (event) => get().applyEvent(event),
      (error) => set({ error: error.message }),
    );
  },

  detach: () => {
    activeStream?.close();
    activeStream = null;
  },

  applyEvent: (event) => {
    const state = get();
    switch (event.event) {
      case 'execution.queued':
        set({ status: 'QUEUED' });
        break;

      case 'execution.started':
        set({
          status: 'RUNNING',
          progress: { ...state.progress, total: event.node_count ?? 0 },
        });
        break;

      case 'execution.progress':
        set({
          progress: {
            completed: event.completed ?? 0,
            failed: event.failed ?? 0,
            skipped: event.skipped ?? 0,
            total: event.total ?? state.progress.total,
            percent: event.percent ?? 0,
            running: event.running ?? [],
          },
        });
        break;

      case 'execution.paused':
        set({ status: 'PAUSED' });
        break;

      case 'execution.resumed':
        set({ status: 'RUNNING' });
        break;

      case 'execution.stopping':
        set({ status: 'STOPPING' });
        break;

      case 'execution.finished':
        set({
          status: (event.status as ExecutionStatus) ?? 'COMPLETED',
          metrics: event.metrics ?? state.metrics,
          error: event.error ?? state.error,
        });
        // The run is over; release the connection.
        get().detach();
        break;

      case 'node.started':
        set({
          nodeStates: {
            ...state.nodeStates,
            [event.node_id]: {
              nodeId: event.node_id,
              nodeName: event.node_name,
              status: 'running',
              iteration: event.iteration,
            },
          },
        });
        break;

      case 'node.finished':
        set({
          nodeStates: {
            ...state.nodeStates,
            [event.node_id]: {
              ...(state.nodeStates[event.node_id] ?? { nodeId: event.node_id }),
              nodeId: event.node_id,
              nodeName: event.node_name,
              status: toNodeStatus(event.status),
              durationMs: event.duration_ms,
              error: event.error,
              errorCode: event.error_code,
            },
          },
        });
        break;

      case 'node.retry':
        set({
          nodeStates: {
            ...state.nodeStates,
            [event.node_id]: {
              ...(state.nodeStates[event.node_id] ?? { nodeId: event.node_id }),
              nodeId: event.node_id,
              status: 'running',
              attempt: event.attempt,
              error: event.error,
            },
          },
        });
        break;

      case 'node.skipped':
        set({
          nodeStates: {
            ...state.nodeStates,
            [event.node_id]: {
              ...(state.nodeStates[event.node_id] ?? { nodeId: event.node_id }),
              nodeId: event.node_id,
              status: 'skipped',
            },
          },
        });
        break;

      case 'log':
        get().appendLog({
          id: event.sequence,
          sequence: event.log_sequence ?? event.sequence,
          level: event.level ?? 'INFO',
          message: event.message ?? '',
          node_id: event.node_id ?? null,
          at: event.at,
        });
        break;

      default:
        break;
    }
  },

  appendLog: (log) =>
    set((state) => {
      const logs = [...state.logs, log];
      return { logs: logs.length > MAX_LOG_LINES ? logs.slice(-MAX_LOG_LINES) : logs };
    }),

  // ---------------------------------------------------------------- fetching
  hydrate: (detail) => {
    const nodeStates: Record<number, NodeRunState> = {};
    for (const record of detail.node_executions ?? []) {
      nodeStates[record.node_id] = {
        nodeId: record.node_id,
        status: toNodeStatus(record.status),
        durationMs: record.duration_ms ?? undefined,
        error: record.error ?? undefined,
        errorCode: record.error_code ?? undefined,
        attempt: record.retry_count,
        iteration: record.iteration,
      };
    }
    const values = Object.values(nodeStates);
    const completed = values.filter((n) => n.status === 'completed').length;
    const failed = values.filter((n) => n.status === 'failed').length;
    const skipped = values.filter((n) => n.status === 'skipped').length;
    const total = values.length || detail.node_executions?.length || 0;

    set({
      executionId: detail.id,
      workflowId: detail.workflow_id,
      status: detail.status,
      nodeStates,
      metrics: detail.metrics ?? {},
      error: detail.error ?? null,
      progress: {
        completed,
        failed,
        skipped,
        total,
        percent: total ? Math.round(((completed + failed + skipped) / total) * 1000) / 10 : 0,
        running: values.filter((n) => n.status === 'running').map((n) => n.nodeId),
      },
    });
  },

  loadExecution: async (executionId) => {
    try {
      const detail = await getExecution(executionId);
      get().hydrate(detail);
      if (!isTerminal(detail.status)) get().attach(executionId);
    } catch (error) {
      set({ error: (error as Error).message });
    }
  },

  loadLogs: async (executionId) => {
    try {
      const response = await getLogs(executionId);
      set({ logs: (response.items ?? []).slice(-MAX_LOG_LINES) });
    } catch (error) {
      set({ error: (error as Error).message });
    }
  },

  loadHistory: async (query = {}) => {
    set({ historyLoading: true });
    try {
      const response = await listExecutions(query);
      set({
        history: response.items ?? [],
        historyTotal: response.total ?? 0,
        historyLoading: false,
      });
    } catch (error) {
      set({ error: (error as Error).message, historyLoading: false });
    }
  },

  reset: () => {
    get().detach();
    set({
      executionId: null,
      status: null,
      nodeStates: {},
      logs: [],
      progress: { ...EMPTY_PROGRESS },
      metrics: {},
      error: null,
      isBusy: false,
    });
  },
}));
