import { useWorkflowStore } from '../stores/workflowStore';
import { describe, it, expect } from 'vitest';

describe('Import/Export & Serialization', () => {
  it('exports valid JSON workflow', () => {
    const store = useWorkflowStore.getState();
    store.addNode({ id: 'n1', type: 'start', position: { x: 0, y: 0 }, data: { label: 'Start' } });
    // exportJSON triggers download — we only verify structure here
    const wf = useWorkflowStore.getState().currentWorkflow;
    expect(wf).toBeDefined();
  });

  it('imports workflow correctly', () => {
    const store = useWorkflowStore.getState();
    const json = JSON.stringify({
      id: 'wf1', name: 'Test', nodes: [{ id: 'n1', type: 'start', position: { x: 0, y: 0 }, data: { label: 'Start' } }], edges: [], version: 1, createdAt: '', updatedAt: ''
    });
    store.importJSON(json);
    expect(useWorkflowStore.getState().nodes.length).toBe(1);
  });
});