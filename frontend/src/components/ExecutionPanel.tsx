import React from 'react';
import { useWorkflowStore } from '../stores/workflowStore';

export const ExecutionPanel: React.FC = () => {
  const { executionStates, resetExecution } = useWorkflowStore();

  return (
    <div style={{ width: 280, background: '#0f172a', color: '#e2e8f0', padding: 16, overflowY: 'auto' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h3>Execution</h3>
        <button onClick={resetExecution} style={{ fontSize: 12 }}>Reset</button>
      </div>
      {Object.keys(executionStates).length === 0 && <p>No executions yet</p>}
      {Object.entries(executionStates).map(([nodeId, state]) => (
        <div key={nodeId} style={{ marginBottom: 12, padding: 8, background: '#1e2937', borderRadius: 6 }}>
          <div><strong>{nodeId}</strong></div>
          <div>Status: {state.status}</div>
          {state.duration && <div>Duration: {state.duration}ms</div>}
          {state.error && <div style={{ color: '#f87171' }}>Error: {state.error}</div>}
        </div>
      ))}
    </div>
  );
};