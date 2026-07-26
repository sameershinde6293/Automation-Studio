import React, { useEffect, useState } from 'react';
import { WorkflowEditor as Canvas } from './WorkflowCanvas';
import { NodePalette } from './NodePalette';
import { ExecutionPanel } from './ExecutionPanel';
import { useWorkflowStore } from '../stores/workflowStore';
import { useExecutionStore } from '../stores/executionStore';

export const WorkflowEditor: React.FC = () => {
  const [showExecution, setShowExecution] = useState(true);
  const { saveWorkflow, exportJSON, nodeIdMap, lastSaveError } = useWorkflowStore();
  const setIdMap = useExecutionStore((s) => s.setIdMap);

  // Keep the execution store's id map in step with the last save/load, so live
  // node events can be matched back to canvas nodes.
  useEffect(() => {
    setIdMap(nodeIdMap ?? {});
  }, [nodeIdMap, setIdMap]);

  return (
    <div style={{ display: 'flex', height: '100vh' }}>
      <NodePalette />
      <div style={{ flex: 1, position: 'relative' }}>
        <Canvas />
        <div style={{ position: 'absolute', top: 10, right: 10, zIndex: 10, display: 'flex', gap: 6 }}>
          <button onClick={saveWorkflow}>Save</button>
          <button onClick={exportJSON}>Export JSON</button>
          <button onClick={() => setShowExecution(!showExecution)}>
            {showExecution ? 'Hide' : 'Show'} Execution
          </button>
        </div>
        {lastSaveError && (
          <div
            data-testid="save-error"
            style={{
              position: 'absolute',
              bottom: 10,
              left: 10,
              zIndex: 10,
              background: '#450a0a',
              border: '1px solid #7f1d1d',
              color: '#fca5a5',
              padding: '6px 10px',
              borderRadius: 6,
              fontSize: 12,
            }}
          >
            Save issue: {lastSaveError}
          </div>
        )}
      </div>
      {showExecution && <ExecutionPanel />}
    </div>
  );
};
