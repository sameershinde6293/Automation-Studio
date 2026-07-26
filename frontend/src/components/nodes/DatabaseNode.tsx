import React from 'react';
import { Database } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * Database node.
 *
 * Config fields mirror the backend schema for `database`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const DatabaseNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Database size={16} />}
    color="#0ea5e9"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'query', label: 'Query', type: 'text' },
      { key: 'max_rows', label: 'Max Rows', type: 'number', default: 1000 },
    ]}
  />
);
