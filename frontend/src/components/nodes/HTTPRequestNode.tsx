import React from 'react';
import { Globe } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * HTTP Request node.
 *
 * Config fields mirror the backend schema for `httpRequest`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const HTTPRequestNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Globe size={16} />}
    color="#06b6d4"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'url', label: 'URL', type: 'text' },
      { key: 'method', label: 'Method', type: 'text', default: 'GET' },
      { key: 'timeout', label: 'Timeout (s)', type: 'number', default: 30 },
    ]}
  />
);
