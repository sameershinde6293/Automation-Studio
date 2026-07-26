import React from 'react';
import { Play, Pause, Square, RotateCcw, XCircle } from 'lucide-react';
import { useExecutionStore } from '../../stores/executionStore';
import { isTerminal } from '../../api/executionApi';

interface Props {
  workflowId?: number | string | null;
  onBeforeRun?: () => Promise<void> | void;
}

/**
 * Run / pause / resume / stop controls.
 *
 * Button availability is derived from the execution status so the UI cannot
 * offer an action the backend would reject with a 409.
 */
export const ExecutionControls: React.FC<Props> = ({ workflowId, onBeforeRun }) => {
  const { executionId, status, isBusy, run, pause, resume, stop, cancel, replay } =
    useExecutionStore();

  const running = status === 'RUNNING' || status === 'QUEUED' || status === 'PENDING';
  const paused = status === 'PAUSED';
  const transitioning = status === 'PAUSING' || status === 'STOPPING';
  const finished = isTerminal(status);
  const active = running || paused || transitioning;

  const handleRun = async () => {
    if (!workflowId) return;
    await onBeforeRun?.();
    await run(workflowId);
  };

  const button = (
    key: string,
    label: string,
    icon: React.ReactNode,
    onClick: () => void,
    disabled: boolean,
    tone: string,
  ) => (
    <button
      key={key}
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      disabled={disabled}
      data-testid={`execution-${key}`}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        padding: '6px 12px',
        borderRadius: 6,
        border: '1px solid #334155',
        background: disabled ? '#1e293b' : tone,
        color: disabled ? '#64748b' : '#f8fafc',
        cursor: disabled ? 'not-allowed' : 'pointer',
        fontSize: 13,
      }}
    >
      {icon}
      {label}
    </button>
  );

  return (
    <div
      data-testid="execution-controls"
      style={{ display: 'flex', gap: 8, alignItems: 'center' }}
    >
      {button(
        'run',
        'Run',
        <Play size={14} />,
        handleRun,
        !workflowId || active || isBusy,
        '#16a34a',
      )}

      {paused
        ? button('resume', 'Resume', <Play size={14} />, resume, isBusy, '#2563eb')
        : button(
            'pause',
            'Pause',
            <Pause size={14} />,
            pause,
            !running || isBusy,
            '#d97706',
          )}

      {button(
        'stop',
        'Stop',
        <Square size={14} />,
        stop,
        !active || isBusy,
        '#dc2626',
      )}

      {button(
        'cancel',
        'Force cancel',
        <XCircle size={14} />,
        cancel,
        !active || isBusy,
        '#7f1d1d',
      )}

      {button(
        'replay',
        'Replay',
        <RotateCcw size={14} />,
        () => executionId && replay(executionId),
        !executionId || !finished,
        '#4f46e5',
      )}

      {status && (
        <span
          data-testid="execution-status"
          style={{
            marginLeft: 8,
            fontSize: 12,
            padding: '3px 8px',
            borderRadius: 999,
            background: '#1e293b',
            color: '#e2e8f0',
            border: '1px solid #334155',
          }}
        >
          {status}
        </span>
      )}
    </div>
  );
};
