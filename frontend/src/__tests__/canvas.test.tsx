import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { WorkflowEditor } from '../components/WorkflowCanvas';

describe('WorkflowCanvas', () => {
  it('renders without crashing', () => {
    const { container } = render(<WorkflowEditor />);
    expect(container.querySelector('.react-flow')).toBeTruthy();
  });
});