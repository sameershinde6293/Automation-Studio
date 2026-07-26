import React from 'react';
import { Repeat } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * Loop node.
 *
 * Config fields mirror the backend schema for `loop`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const LoopNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Repeat size={16} />}
    color="#f59e0b"
    inputs={['in']}
    outputs={['loop', 'done']}
    configFields={[
      { key: 'mode', label: 'Mode', type: 'text', default: 'collect' },
      { key: 'count', label: 'Count', type: 'number' },
      { key: 'max_iterations', label: 'Max Iterations', type: 'number' },
    ]}
  />
);
