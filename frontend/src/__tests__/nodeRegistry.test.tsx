/**
 * M5: node component registry.
 *
 * Regression guard for a defect the M5 audit surfaced: 20 of the 22 node
 * component files were committed **empty** in M3/M4. Nothing caught it because
 * `App.tsx` never imported the editor, so the modules were never bundled and
 * `vite build` never resolved them. The moment the editor was actually mounted
 * the build failed with "AIChatNode is not exported by AIChatNode.tsx".
 *
 * These tests assert every registered node type resolves to a real, renderable
 * component, so an empty or missing module fails here rather than at build.
 */

import React from 'react';
import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { ReactFlowProvider } from 'reactflow';
import { nodeTypes } from '../components/nodes';

/** Every node type the backend palette exposes to the editor. */
const EXPECTED_TYPES = [
  'start', 'end', 'aiChat', 'aiCompletion', 'prompt', 'variable',
  'condition', 'loop', 'delay', 'httpRequest', 'webhook', 'python',
  'javascript', 'database', 'email', 'file', 'folder',
  'imageGeneration', 'tts', 'stt', 'ffmpeg', 'mediaProcessing',
];

describe('node registry', () => {
  it('registers every expected node type', () => {
    expect(Object.keys(nodeTypes).sort()).toEqual([...EXPECTED_TYPES].sort());
  });

  it.each(EXPECTED_TYPES)('%s resolves to a component', (type) => {
    const Component = (nodeTypes as Record<string, unknown>)[type];
    expect(Component, `${type} is not registered`).toBeDefined();
    expect(typeof Component, `${type} is not a component`).toBe('function');
  });

  it.each(EXPECTED_TYPES)('%s renders without throwing', (type) => {
    const Component = (nodeTypes as Record<string, any>)[type];
    const { container } = render(
      <ReactFlowProvider>
        <Component
          id={`node-${type}`}
          type={type}
          data={{ label: type, config: {} }}
          selected={false}
          zIndex={1}
          isConnectable
          xPos={0}
          yPos={0}
          dragging={false}
        />
      </ReactFlowProvider>,
    );
    expect(container.firstChild).not.toBeNull();
  });

  it('renders the node label', () => {
    const Component = (nodeTypes as Record<string, any>).aiChat;
    const { getByText } = render(
      <ReactFlowProvider>
        <Component
          id="n1"
          type="aiChat"
          data={{ label: 'My Chat Node', config: {} }}
          selected={false}
          zIndex={1}
          isConnectable
          xPos={0}
          yPos={0}
          dragging={false}
        />
      </ReactFlowProvider>,
    );
    expect(getByText('My Chat Node')).toBeInTheDocument();
  });
});
