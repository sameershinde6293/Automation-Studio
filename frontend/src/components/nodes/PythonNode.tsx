import React from 'react';
import { Code2 } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * Python node.
 *
 * Config fields mirror the backend schema for `python`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const PythonNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Code2 size={16} />}
    color="#64748b"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'code', label: 'Code', type: 'text' },
      { key: 'timeout', label: 'Timeout (s)', type: 'number', default: 30 },
    ]}
  />
);
