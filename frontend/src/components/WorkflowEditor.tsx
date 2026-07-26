import React, { useState } from 'react';
import { WorkflowEditor as Canvas } from './WorkflowCanvas';
import { NodePalette } from './NodePalette';
import { ExecutionPanel } from './ExecutionPanel';
import { useWorkflowStore } from '../stores/workflowStore';

export const WorkflowEditor: React.FC = () => {
  const [showExecution, setShowExecution] = useState(false);
  const { saveWorkflow, exportJSON } = useWorkflowStore();

  return (
    <div style={{ display: 'flex', height: '100vh' }}>
      <NodePalette />
      <div style={{ flex: 1, position: 'relative' }}>
        <Canvas />
        <div style={{ position: 'absolute', top: 10, right: 10, zIndex: 10 }}>
          <button onClick={saveWorkflow}>Save</button>
          <button onClick={exportJSON}>Export JSON</button>
          <button onClick={() => setShowExecution(!showExecution)}>
            {showExecution ? 'Hide' : 'Show'} Execution
          </button>
        </div>
      </div>
      {showExecution && <ExecutionPanel />}
    </div>
  );
};