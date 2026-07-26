import { useWorkflowStore } from '../stores/workflowStore';
import { describe, it, expect, beforeEach } from 'vitest';

describe('WorkflowStore', () => {
  beforeEach(() => {
    useWorkflowStore.setState({
      nodes: [], edges: [], executionStates: {}, history: [], historyIndex: -1, isDirty: false, currentWorkflow: null
    });
  });

  it('adds and deletes nodes', () => {
    const store = useWorkflowStore.getState();
    store.addNode({ id: 'n1', type: 'start', position: { x: 0, y: 0 }, data: { label: 'Start' } });
    expect(useWorkflowStore.getState().nodes.length).toBe(1);
    store.deleteNode('n1');
    expect(useWorkflowStore.getState().nodes.length).toBe(0);
  });

  it('supports undo/redo', () => {
    const store = useWorkflowStore.getState();
    store.addNode({ id: 'n1', type: 'start', position: { x: 0, y: 0 }, data: { label: 'Start' } });
    store.undo();
    expect(useWorkflowStore.getState().nodes.length).toBe(0);
    store.redo();
    expect(useWorkflowStore.getState().nodes.length).toBe(1);
  });

  it('tracks dirty state and autosave', () => {
    const store = useWorkflowStore.getState();
    store.addNode({ id: 'n1', type: 'start', position: { x: 0, y: 0 }, data: { label: 'Start' } });
    expect(useWorkflowStore.getState().isDirty).toBe(true);
  });

  it('serializes and deserializes correctly', () => {
    const store = useWorkflowStore.getState();
    store.addNode({ id: 'n1', type: 'start', position: { x: 100, y: 100 }, data: { label: 'Start', config: { name: 'Main' } } });
    const json = JSON.stringify(useWorkflowStore.getState().nodes);
    const parsed = JSON.parse(json);
    expect(parsed[0].data.config.name).toBe('Main');
  });
});