import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { BaseNode } from '../components/nodes/BaseNode';
import { useWorkflowStore } from '../stores/workflowStore';

describe('BaseNode', () => {
  beforeEach(() => {
    useWorkflowStore.setState({ nodes: [], edges: [] });
  });

  it('renders with label and config input', () => {
    const data = { label: 'Test Node', config: { model: 'gpt-4' } };
    render(<BaseNode id="n1" data={data} icon={<span>Icon</span>} color="#3b82f6" configFields={[{ key: 'model', label: 'Model', type: 'text' }]} />);
    expect(screen.getByText('Test Node')).toBeInTheDocument();
    expect(screen.getByDisplayValue('gpt-4')).toBeInTheDocument();
  });

  it('writes config changes back to the workflow store', () => {
    // BaseNode is a controlled component: its input value comes from the
    // store, so the node must exist there for the edit to be observable.
    const data = { label: 'Test', config: {} };
    useWorkflowStore.setState({
      nodes: [{ id: 'n1', type: 'start', position: { x: 0, y: 0 }, data } as any],
      edges: [],
    });

    const { container } = render(
      <BaseNode
        id="n1"
        data={data}
        icon={<span />}
        color="#fff"
        configFields={[{ key: 'model', label: 'Model', type: 'text' }]}
      />,
    );
    const input = container.querySelector('input')!;
    fireEvent.change(input, { target: { value: 'Updated' } });

    const stored = useWorkflowStore.getState().nodes[0] as any;
    expect(stored.data.config.model).toBe('Updated');
  });
});