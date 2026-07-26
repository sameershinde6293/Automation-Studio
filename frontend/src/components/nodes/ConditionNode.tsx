import React from 'react';
import { GitBranch } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * Condition node.
 *
 * Config fields mirror the backend schema for `condition`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const ConditionNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<GitBranch size={16} />}
    color="#f59e0b"
    inputs={['in']}
    outputs={['true', 'false']}
    configFields={[
      { key: 'left', label: 'Left', type: 'text' },
      { key: 'operator', label: 'Operator', type: 'text', default: 'truthy' },
      { key: 'right', label: 'Right', type: 'text' },
    ]}
  />
);
