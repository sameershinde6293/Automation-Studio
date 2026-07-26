import React, { useEffect, useState } from 'react';
import { useExecutionStore } from '../stores/executionStore';
import type { NodeRunStatus } from '../stores/executionStore';
import { useWorkflowStore } from '../stores/workflowStore';
import { ExecutionControls } from './execution/ExecutionControls';
import { ExecutionProgressBar } from './execution/ExecutionProgress';
import { LogViewer } from './execution/LogViewer';
import { ExecutionHistoryPanel } from './execution/ExecutionHistoryPanel';

type Tab = 'nodes' | 'logs' | 'history';

const STATUS_COLOURS: Record<NodeRunStatus, string> = {
  idle: '#94a3b8',
  running: '#3b82f6',
  completed: '#22c55e',
  failed: '#ef4444',
  skipped: '#64748b',
  cancelled: '#f59e0b',
};

/**
 * Execution side panel.
 *
 * M3 shipped a placeholder here that ran an empty `setInterval` and rendered
 * state nothing ever populated. This is now driven entirely by the backend
 * SSE stream via `executionStore`.
 */
export const ExecutionPanel: React.FC = () => {
  const [tab, setTab] = useState<Tab>('nodes');
  const { nodeStates, error, executionId, status, detach, editorIdFor } =
    useExecutionStore();
  const nodes = useWorkflowStore((s) => s.nodes);
  const currentWorkflow = useWorkflowStore((s) => s.currentWorkflow);

  // Always release the stream when the panel unmounts, otherwise a closed
  // panel keeps an SSE connection (and its timers) alive.
  useEffect(() => () => detach(), [detach]);

  const entries = Object.values(nodeStates);

  const labelFor = (backendId: number, fallback?: string) => {
    const editorId = editorIdFor(backendId);
    const node = editorId ? nodes.find((n) => n.id === editorId) : undefined;
    return node?.data?.label ?? fallback ?? `Node ${backendId}`;
  };

  return (
    <div
      data-testid="execution-panel"
      style={{
        width: 340,
        background: '#0f172a',
        color: '#e2e8f0',
        padding: 14,
        overflowY: 'auto',
        borderLeft: '1px solid #334155',
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h3 style={{ margin: 0, fontSize: 15 }}>Execution</h3>
        {executionId && (
          <span style={{ fontSize: 11, color: '#64748b' }}>#{executionId}</span>
        )}
      </div>

      <ExecutionControls workflowId={currentWorkflow?.id as any} />
      <ExecutionProgressBar />

      {error && (
        <div
          data-testid="execution-error"
          style={{
            background: '#450a0a',
            border: '1px solid #7f1d1d',
            color: '#fca5a5',
            padding: 8,
            borderRadius: 6,
            fontSize: 11,
          }}
        >
          {error}
        </div>
      )}

      <div style={{ display: 'flex', gap: 4, borderBottom: '1px solid #1e293b' }}>
        {(['nodes', 'logs', 'history'] as Tab[]).map((name) => (
          <button
            key={name}
            type="button"
            data-testid={`tab-${name}`}
            onClick={() => setTab(name)}
            style={{
              flex: 1,
              padding: '6px 4px',
              background: 'none',
              border: 'none',
              borderBottom: tab === name ? '2px solid #3b82f6' : '2px solid transparent',
              color: tab === name ? '#e2e8f0' : '#64748b',
              cursor: 'pointer',
              fontSize: 12,
              textTransform: 'capitalize',
            }}
          >
            {name}
          </button>
        ))}
      </div>

      {tab === 'nodes' && (
        <div data-testid="node-status-list">
          {entries.length === 0 && (
            <p style={{ color: '#64748b', fontSize: 12 }}>
              {status ? 'Waiting for node updates…' : 'No active execution.'}
            </p>
          )}
          {entries.map((state) => (
            <div
              key={state.nodeId}
              data-testid="node-status-item"
              style={{
                marginBottom: 8,
                padding: 8,
                background: '#111c30',
                borderRadius: 6,
                borderLeft: `3px solid ${STATUS_COLOURS[state.status]}`,
              }}
            >
              <div style={{ fontSize: 12, fontWeight: 600 }}>
                {labelFor(state.nodeId, state.nodeName)}
              </div>
              <div style={{ fontSize: 11, color: STATUS_COLOURS[state.status] }}>
                {state.status.toUpperCase()}
                {state.attempt ? ` · attempt ${state.attempt}` : ''}
                {state.iteration ? ` · iter ${state.iteration}` : ''}
              </div>
              {state.durationMs !== undefined && (
                <div style={{ fontSize: 10, color: '#64748b' }}>
                  {Math.round(state.durationMs)}ms
                </div>
              )}
              {state.error && (
                <div style={{ color: '#f87171', fontSize: 10, marginTop: 2 }}>
                  {state.errorCode ? `[${state.errorCode}] ` : ''}
                  {state.error}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {tab === 'logs' && <LogViewer />}
      {tab === 'history' && (
        <ExecutionHistoryPanel workflowId={currentWorkflow?.id as any} />
      )}
    </div>
  );
};
