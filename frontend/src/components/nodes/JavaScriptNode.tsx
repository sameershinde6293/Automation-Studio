import React from 'react';
import { Braces } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * JavaScript node.
 *
 * Config fields mirror the backend schema for `javascript`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const JavaScriptNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Braces size={16} />}
    color="#64748b"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'code', label: 'Code', type: 'text' },
      { key: 'timeout', label: 'Timeout (s)', type: 'number', default: 30 },
    ]}
  />
);
