import React from 'react';
import { Clock } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * Delay node.
 *
 * Config fields mirror the backend schema for `delay`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const DelayNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Clock size={16} />}
    color="#f59e0b"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'seconds', label: 'Seconds', type: 'number', default: 1 },
    ]}
  />
);
