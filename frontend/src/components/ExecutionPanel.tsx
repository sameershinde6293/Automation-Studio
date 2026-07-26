import React, { useEffect } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';

export const ExecutionPanel: React.FC = () => {
  const { executionStates, resetExecution, nodes } = useWorkflowStore();

  // Simulate live execution animation (for demo until real backend execution stream)
  useEffect(() => {
    const interval = setInterval(() => {
      const running = Object.values(executionStates).find(s => s.status === 'running');
      if (running) {
        // progress simulation
      }
    }, 800);
    return () => clearInterval(interval);
  }, [executionStates]);

  const getStatusColor = (status: string) => {
    if (status === 'running') return '#3b82f6';
    if (status === 'completed') return '#22c55e';
    if (status === 'failed') return '#ef4444';
    if (status === 'skipped') return '#64748b';
    return '#94a3b8';
  };

  return (
    <div style={{ width: 300, background: '#0f172a', color: '#e2e8f0', padding: 16, overflowY: 'auto', borderLeft: '1px solid #334155' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <h3>Execution</h3>
        <button onClick={resetExecution} style={{ fontSize: 12 }}>Reset</button>
      </div>

      {Object.keys(executionStates).length === 0 && <p style={{ color: '#64748b' }}>No active execution</p>}

      {Object.entries(executionStates).map(([nodeId, state]) => {
        const node = nodes.find(n => n.id === nodeId);
        return (
          <div key={nodeId} style={{ marginBottom: 10, padding: 10, background: '#1e2937', borderRadius: 6, borderLeft: `4px solid ${getStatusColor(state.status)}` }}>
            <div><strong>{node?.data?.label || nodeId}</strong></div>
            <div style={{ fontSize: 12, color: getStatusColor(state.status) }}>{state.status.toUpperCase()}</div>
            {state.duration && <div style={{ fontSize: 11 }}>Duration: {state.duration}ms</div>}
            {state.error && <div style={{ color: '#f87171', fontSize: 11 }}>{state.error}</div>}
          </div>
        );
      })}
    </div>
  );
};