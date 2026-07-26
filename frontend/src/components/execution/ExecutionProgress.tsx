import React from 'react';
import { useExecutionStore } from '../../stores/executionStore';

/** Progress bar plus counters and the currently running node(s). */
export const ExecutionProgressBar: React.FC = () => {
  const { progress, status, metrics, nodeStates } = useExecutionStore();

  const runningNames = progress.running
    .map((id) => nodeStates[id]?.nodeName ?? `#${id}`)
    .slice(0, 3);

  const barColour =
    status === 'FAILED'
      ? '#ef4444'
      : status === 'CANCELLED'
        ? '#f59e0b'
        : status === 'COMPLETED'
          ? '#22c55e'
          : '#3b82f6';

  return (
    <div data-testid="execution-progress" style={{ padding: '8px 0' }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          fontSize: 12,
          color: '#94a3b8',
          marginBottom: 4,
        }}
      >
        <span data-testid="progress-percent">{progress.percent}%</span>
        <span data-testid="progress-counts">
          {progress.completed}/{progress.total} done
          {progress.failed > 0 && ` · ${progress.failed} failed`}
          {progress.skipped > 0 && ` · ${progress.skipped} skipped`}
        </span>
      </div>

      <div
        role="progressbar"
        aria-valuenow={progress.percent}
        aria-valuemin={0}
        aria-valuemax={100}
        style={{
          height: 6,
          background: '#1e293b',
          borderRadius: 999,
          overflow: 'hidden',
        }}
      >
        <div
          data-testid="progress-fill"
          style={{
            width: `${Math.min(100, Math.max(0, progress.percent))}%`,
            height: '100%',
            background: barColour,
            transition: 'width 200ms ease',
          }}
        />
      </div>

      {runningNames.length > 0 && (
        <div data-testid="running-nodes" style={{ fontSize: 11, color: '#60a5fa', marginTop: 6 }}>
          Running: {runningNames.join(', ')}
          {progress.running.length > 3 && ` +${progress.running.length - 3} more`}
        </div>
      )}

      {typeof metrics.duration_ms === 'number' && (
        <div style={{ fontSize: 11, color: '#64748b', marginTop: 4 }}>
          Duration: {Math.round(metrics.duration_ms)}ms
          {metrics.total_tokens ? ` · ${metrics.total_tokens} tokens` : ''}
          {metrics.cost_usd ? ` · $${Number(metrics.cost_usd).toFixed(4)}` : ''}
        </div>
      )}
    </div>
  );
};
