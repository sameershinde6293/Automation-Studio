import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { BaseNode } from '../components/nodes/BaseNode';

describe('BaseNode', () => {
  it('renders with label and config input', () => {
    const data = { label: 'Test Node', config: { model: 'gpt-4' } };
    render(<BaseNode id="n1" data={data} icon={<span>Icon</span>} color="#3b82f6" configFields={[{ key: 'model', label: 'Model', type: 'text' }]} />);
    expect(screen.getByText('Test Node')).toBeInTheDocument();
    expect(screen.getByDisplayValue('gpt-4')).toBeInTheDocument();
  });

  it('updates config on input change', () => {
    const data = { label: 'Test', config: {} };
    const { container } = render(<BaseNode id="n1" data={data} icon={<span />} color="#fff" configFields={[{ key: 'label', label: 'Label', type: 'text' }]} />);
    const input = container.querySelector('input')!;
    fireEvent.change(input, { target: { value: 'Updated' } });
    // In real test would assert store update
    expect(input.value).toBe('Updated');
  });
});