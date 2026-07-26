/**
 * Execution API client.
 *
 * Wraps the M4 backend execution surface: run, pause, resume, stop, cancel,
 * history, logs, timeline and the SSE live stream. Kept free of React so it can
 * be unit-tested directly and reused outside the editor.
 */

export const API_ROOT =
  (import.meta as any)?.env?.VITE_API_BASE ?? 'http://localhost:8000/api';

export type ExecutionStatus =
  | 'PENDING'
  | 'QUEUED'
  | 'RUNNING'
  | 'PAUSED'
  | 'PAUSING'
  | 'STOPPING'
  | 'COMPLETED'
  | 'FAILED'
  | 'CANCELLED'
  | 'SKIPPED';

export const TERMINAL_STATUSES: ExecutionStatus[] = [
  'COMPLETED',
  'FAILED',
  'CANCELLED',
  'SKIPPED',
];

export const isTerminal = (status?: string | null): boolean =>
  !!status && TERMINAL_STATUSES.includes(status as ExecutionStatus);

export const isActive = (status?: string | null): boolean =>
  !!status && !isTerminal(status);

export interface NodeExecutionRecord {
  node_id: number;
  status: ExecutionStatus | null;
  output_data?: unknown;
  error?: string | null;
  error_code?: string | null;
  retry_count: number;
  duration_ms?: number | null;
  queued_ms?: number | null;
  started_at?: string | null;
  finished_at?: string | null;
  iteration: number;
}

export interface ExecutionSummary {
  id: number;
  workflow_id: number;
  workflow_name?: string | null;
  status: ExecutionStatus | null;
  trigger?: string | null;
  priority: number;
  error?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  parent_execution_id?: number | null;
  replay_of?: string | null;
  metrics: Record<string, any>;
}

export interface ExecutionDetail extends ExecutionSummary {
  node_executions: NodeExecutionRecord[];
  state: Record<string, any>;
  input_data: Record<string, any>;
  log_count: number;
  is_running: boolean;
  is_paused: boolean;
}

export interface LogRecord {
  id: number;
  sequence: number;
  level: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR';
  message: string;
  node_id?: number | null;
  context?: Record<string, any> | null;
  at?: string | null;
}

export interface ExecutionEvent {
  execution_id: number;
  event: string;
  sequence: number;
  at: string;
  [key: string]: any;
}

export interface RunOptions {
  trigger?: string;
  priority?: number;
  inputData?: Record<string, any>;
  wait?: boolean;
}

export interface HistoryQuery {
  workflowId?: number;
  status?: string[];
  trigger?: string;
  search?: string;
  skip?: number;
  limit?: number;
}

export class ExecutionApiError extends Error {
  status: number;
  code?: string;
  details?: unknown;

  constructor(message: string, status: number, code?: string, details?: unknown) {
    super(message);
    this.name = 'ExecutionApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });

  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    let code: string | undefined;
    let details: unknown;
    try {
      const body = await response.json();
      // The backend uses a stable { error: { code, message, details } } envelope.
      message = body?.error?.message ?? body?.detail ?? message;
      code = body?.error?.code;
      details = body?.error?.details;
    } catch {
      /* non-JSON error body; keep the generic message */
    }
    throw new ExecutionApiError(message, response.status, code, details);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** Start a workflow run. */
export async function runWorkflow(
  workflowId: number | string,
  options: RunOptions = {},
): Promise<{ execution_id: number; status: string; stream_url?: string }> {
  return request(`/workflows/${workflowId}/executions`, {
    method: 'POST',
    body: JSON.stringify({
      trigger: options.trigger ?? 'manual',
      wait: options.wait ?? false,
      priority: options.priority,
      input_data: options.inputData,
    }),
  });
}

export const pauseExecution = (id: number) =>
  request<{ changed: boolean; message: string }>(`/executions/${id}/pause`, {
    method: 'POST',
  });

export const resumeExecution = (id: number) =>
  request<{ changed: boolean; message: string }>(`/executions/${id}/resume`, {
    method: 'POST',
  });

export const stopExecution = (id: number) =>
  request<{ changed: boolean; message: string }>(`/executions/${id}/stop`, {
    method: 'POST',
  });

/** Hard cancel (kills in-flight nodes) — distinct from the graceful stop. */
export const cancelExecution = (id: number) =>
  request<{ cancelled: boolean }>(`/workflows/executions/${id}/cancel`, {
    method: 'POST',
  });

export const getExecution = (id: number) =>
  request<ExecutionDetail>(`/executions/${id}`);

export const getLogs = (id: number, afterSequence = 0, limit = 500) =>
  request<{ items: LogRecord[]; last_sequence: number }>(
    `/executions/${id}/logs?after_sequence=${afterSequence}&limit=${limit}`,
  );

export const getTimeline = (id: number) =>
  request<Record<string, any>>(`/executions/${id}/timeline`);

export const replayExecution = (id: number, start = true) =>
  request<{ execution_id: number }>(`/executions/${id}/replay`, {
    method: 'POST',
    body: JSON.stringify({ start }),
  });

export const resumeFailedExecution = (id: number, start = true) =>
  request<{ execution_id: number }>(`/executions/${id}/resume-failed`, {
    method: 'POST',
    body: JSON.stringify({ start }),
  });

export const getQueueStatus = () =>
  request<Record<string, any>>('/executions/queue');

export function listExecutions(query: HistoryQuery = {}) {
  const params = new URLSearchParams();
  if (query.workflowId !== undefined) params.set('workflow_id', String(query.workflowId));
  (query.status ?? []).forEach((s) => params.append('status', s));
  if (query.trigger) params.set('trigger', query.trigger);
  if (query.search) params.set('search', query.search);
  params.set('skip', String(query.skip ?? 0));
  params.set('limit', String(query.limit ?? 25));

  return request<{
    items: ExecutionSummary[];
    total: number;
    has_more: boolean;
  }>(`/executions?${params.toString()}`);
}

/** Polling fallback for environments without EventSource. */
export const pollEvents = (id: number, afterSequence = 0) =>
  request<{ events: ExecutionEvent[]; last_sequence: number }>(
    `/executions/${id}/events?after_sequence=${afterSequence}`,
  );

export interface StreamHandle {
  close: () => void;
}

/**
 * Subscribe to an execution's live event stream.
 *
 * Uses SSE when available and transparently falls back to polling (Electron
 * builds and test environments may lack EventSource). The returned handle must
 * be closed by the caller to release the connection/timer.
 */
export function streamExecution(
  executionId: number,
  onEvent: (event: ExecutionEvent) => void,
  onError?: (error: Error) => void,
  afterSequence = 0,
): StreamHandle {
  const EventSourceImpl = (globalThis as any).EventSource;

  if (typeof EventSourceImpl === 'function') {
    const source: EventSource = new EventSourceImpl(
      `${API_ROOT}/executions/${executionId}/stream?after_sequence=${afterSequence}`,
    );
    let closed = false;

    const handle = (raw: MessageEvent) => {
      try {
        const parsed = JSON.parse(raw.data) as ExecutionEvent;
        onEvent(parsed);
        if (parsed.event === 'execution.finished') {
          closed = true;
          source.close();
        }
      } catch {
        /* ignore malformed frames rather than tearing down the stream */
      }
    };

    // Named events plus the default channel, so every frame is captured.
    [
      'execution.queued',
      'execution.started',
      'execution.progress',
      'execution.paused',
      'execution.resumed',
      'execution.stopping',
      'execution.finished',
      'node.started',
      'node.finished',
      'node.retry',
      'node.skipped',
      'log',
    ].forEach((name) => source.addEventListener(name, handle as EventListener));
    source.onmessage = handle;
    source.onerror = () => {
      if (closed) return;
      onError?.(new Error('Execution stream disconnected'));
    };

    return {
      close: () => {
        closed = true;
        source.close();
      },
    };
  }

  // --- polling fallback ---------------------------------------------------
  let cursor = afterSequence;
  let stopped = false;

  const tick = async () => {
    if (stopped) return;
    try {
      const batch = await pollEvents(executionId, cursor);
      cursor = batch.last_sequence ?? cursor;
      for (const event of batch.events ?? []) {
        onEvent(event);
        if (event.event === 'execution.finished') stopped = true;
      }
    } catch (error) {
      onError?.(error as Error);
    }
    if (!stopped) timer = setTimeout(tick, 1000);
  };

  let timer: any = setTimeout(tick, 0);
  return {
    close: () => {
      stopped = true;
      clearTimeout(timer);
    },
  };
}
