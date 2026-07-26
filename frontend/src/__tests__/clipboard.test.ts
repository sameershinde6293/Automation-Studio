import { useWorkflowStore } from '../stores/workflowStore';
import { describe, it, expect, beforeEach } from 'vitest';

describe('Clipboard & Selection', () => {
  beforeEach(() => {
    useWorkflowStore.setState({ nodes: [], edges: [] });
  });

  it('copies and pastes nodes', () => {
    const store = useWorkflowStore.getState();
    store.addNode({ id: 'n1', type: 'start', position: { x: 0, y: 0 }, data: { label: 'Start' } });
    // Simulate selection
    (store.nodes[0] as any).selected = true;
    store.copySelected();
    store.paste();
    expect(useWorkflowStore.getState().nodes.length).toBe(2);
  });

  it('duplicates selected nodes', () => {
    const store = useWorkflowStore.getState();
    store.addNode({ id: 'n1', type: 'start', position: { x: 0, y: 0 }, data: { label: 'Start' } });
    (store.nodes[0] as any).selected = true;
    store.duplicateSelected();
    expect(useWorkflowStore.getState().nodes.length).toBe(2);
  });
});