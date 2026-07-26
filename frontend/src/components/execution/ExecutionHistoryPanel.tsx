import React, { useEffect, useState } from 'react';
import { RotateCcw, PlayCircle } from 'lucide-react';
import { useExecutionStore } from '../../stores/executionStore';
import type { ExecutionSummary } from '../../api/executionApi';

const STATUS_COLOURS: Record<string, string> = {
  COMPLETED: '#22c55e',
  FAILED: '#ef4444',
  CANCELLED: '#f59e0b',
  RUNNING: '#3b82f6',
  QUEUED: '#8b5cf6',
  PAUSED: '#eab308',
  PENDING: '#64748b',
};

const FILTERS = ['ALL', 'COMPLETED', 'FAILED', 'CANCELLED', 'RUNNING'] as const;

interface Props {
  workflowId?: number | null;
}

/** Searchable, filterable list of past runs with replay / resume actions. */
export const ExecutionHistoryPanel: React.FC<Props> = ({ workflowId }) => {
  const {
    history,
    historyTotal,
    historyLoading,
    loadHistory,
    loadExecution,
    replay,
    resumeFailed,
  } = useExecutionStore();

  const [filter, setFilter] = useState<(typeof FILTERS)[number]>('ALL');
  const [search, setSearch] = useState('');

  useEffect(() => {
    loadHistory({
      workflowId: workflowId ?? undefined,
      status: filter === 'ALL' ? undefined : [filter],
      search: search.trim() || undefined,
      limit: 25,
    });
    // Debouncing is intentionally omitted: the list is small and the endpoint
    // is indexed on (workflow_id, status).
  }, [workflowId, filter, search, loadHistory]);

  return (
    <div data-testid="history-panel" style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <div style={{ display: 'flex', gap: 6, marginBottom: 6 }}>
        <select
          aria-label="Filter executions by status"
          data-testid="history-filter"
          value={filter}
          onChange={(e) => setFilter(e.target.value as (typeof FILTERS)[number])}
          style={{ fontSize: 11, background: '#1e293b', color: '#e2e8f0', border: '1px solid #334155', borderRadius: 4, padding: '3px 6px' }}
        >
          {FILTERS.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>

        <input
          aria-label="Search executions"
          data-testid="history-search"
          placeholder="Search runs…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ flex: 1, fontSize: 11, background: '#1e293b', color: '#e2e8f0', border: '1px solid #334155', borderRadius: 4, padding: '3px 6px' }}
        />
      </div>

      {historyLoading && (
        <div data-testid="history-loading" style={{ color: '#64748b', fontSize: 12 }}>
          Loading…
        </div>
      )}

      {!historyLoading && history.length === 0 && (
        <div data-testid="history-empty" style={{ color: '#475569', fontSize: 12 }}>
          No executions yet.
        </div>
      )}

      <div style={{ overflowY: 'auto', maxHeight: 300 }}>
        {history.map((item: ExecutionSummary) => (
          <div
            key={item.id}
            data-testid="history-item"
            style={{
              padding: 8,
              marginBottom: 6,
              background: '#111c30',
              border: '1px solid #1e293b',
              borderRadius: 6,
              borderLeft: `3px solid ${STATUS_COLOURS[item.status ?? ''] ?? '#475569'}`,
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <button
                type="button"
                data-testid="history-open"
                onClick={() => loadExecution(item.id)}
                style={{
                  background: 'none',
                  border: 'none',
                  color: '#e2e8f0',
                  cursor: 'pointer',
                  padding: 0,
                  fontSize: 12,
                  fontWeight: 600,
                }}
              >
                #{item.id} {item.workflow_name ?? `Workflow ${item.workflow_id}`}
              </button>
              <span style={{ fontSize: 10, color: STATUS_COLOURS[item.status ?? ''] ?? '#94a3b8' }}>
                {item.status}
              </span>
            </div>

            <div style={{ fontSize: 10, color: '#64748b', marginTop: 2 }}>
              {item.trigger ?? 'manual'}
              {typeof item.metrics?.duration_ms === 'number' &&
                ` · ${Math.round(item.metrics.duration_ms)}ms`}
              {item.replay_of && ` · ${item.replay_of}`}
            </div>

            {item.error && (
              <div data-testid="history-error" style={{ fontSize: 10, color: '#f87171', marginTop: 3 }}>
                {item.error.slice(0, 160)}
              </div>
            )}

            <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
              <button
                type="button"
                aria-label={`Replay execution ${item.id}`}
                data-testid="history-replay"
                onClick={() => replay(item.id)}
                style={actionStyle}
              >
                <RotateCcw size={11} /> Replay
              </button>

              {item.status === 'FAILED' && (
                <button
                  type="button"
                  aria-label={`Resume failed execution ${item.id}`}
                  data-testid="history-resume-failed"
                  onClick={() => resumeFailed(item.id)}
                  style={actionStyle}
                >
                  <PlayCircle size={11} /> Resume failed
                </button>
              )}
            </div>
          </div>
        ))}
      </div>

      <div style={{ fontSize: 10, color: '#475569', marginTop: 4 }}>
        Showing {history.length} of {historyTotal}
      </div>
    </div>
  );
};

const actionStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 4,
  fontSize: 10,
  padding: '3px 8px',
  background: '#1e293b',
  color: '#cbd5e1',
  border: '1px solid #334155',
  borderRadius: 4,
  cursor: 'pointer',
};
