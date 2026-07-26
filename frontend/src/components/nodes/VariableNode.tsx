import React from 'react';
import { Box } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * Variable node.
 *
 * Config fields mirror the backend schema for `variable`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const VariableNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Box size={16} />}
    color="#0ea5e9"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'name', label: 'Name', type: 'text' },
      { key: 'operation', label: 'Operation', type: 'text', default: 'set' },
      { key: 'value', label: 'Value', type: 'text' },
    ]}
  />
);
